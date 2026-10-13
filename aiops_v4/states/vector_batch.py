
"""Bounded replay of once-computed vectors, with lossless typed spill batches."""
from pathlib import Path
from collections import namedtuple
import struct
from array import array
import sys
import zlib
from .codec import pack_delta,unpack_delta,MAX_BYTES,_same

_HEADER=struct.Struct("<III")
_NUMBERS=struct.Struct("<III")
_LENGTH=struct.Struct("<I")
_Page=namedtuple("_Page","metadata markers floats integers")


_GRAIN=('schema_version','group_id','batch','source','view','identity','dimensions')
_FEATURE_META=('kind','metric','stat','transform','unit','semantic_status')
_RULE_VALUES=('last','min','max','mean','delta_sum','rate_mean','change_count','counter_decrease_count','counter_gap_count')


def _copy(value):
    # Retained pages and each public replay own their mutable JSON fields.
    if isinstance(value,dict):return {key:_copy(item) for key,item in value.items()}
    if isinstance(value,list):return [_copy(item) for item in value]
    return value


def _metadata_size(value):
    size=sys.getsizeof(value)
    if isinstance(value,dict):return size+sum(_metadata_size(key)+_metadata_size(item) for key,item in value.items())
    if isinstance(value,list):return size+sum(_metadata_size(item) for item in value)
    return size


def _page_size(page):
    # Numeric buffers report their allocated capacity, including array slack.
    # The smaller metadata tree contains no per-window numeric objects.
    return (sys.getsizeof(page)+_metadata_size(page.metadata)+sys.getsizeof(page.markers)+
            sys.getsizeof(page.floats)+sys.getsizeof(page.integers)+64)


def _compact(rows):
    base=_copy({key:rows[0][key] for key in _GRAIN})
    features=[];feature_ids={};rules=[];rule_ids={};records=[];extras=[]
    markers=bytearray();floats=array('d');integers=array('q')
    def number(value):
        if value is None:markers.append(0)
        elif type(value) is float:markers.append(1);floats.append(value)
        elif type(value) is int and -(2**63)<=value<2**63:markers.append(2);integers.append(value)
        elif type(value) is bool:markers.append(5 if value else 4)
        else:markers.append(3);extras.append(_copy(value))
    for row in rows:
        cells=[]
        for name,cell in row['features'].items():
            key=(name,)+tuple(cell[key] for key in _FEATURE_META)
            if key not in feature_ids:
                feature_ids[key]=len(features);features.append([name,{key:cell[key] for key in _FEATURE_META}])
            cells.append(feature_ids[key]);number(cell['value']);number(cell['raw_value'])
        inputs=[]
        for name,metric in row['rule_inputs'].items():
            metadata={key:value for key,value in metric.items() if key not in _RULE_VALUES and key!='usable'}
            key=(name,)+tuple(sorted(metadata.items()))
            if key not in rule_ids:rule_ids[key]=len(rules);rules.append([name,metadata])
            inputs.append([rule_ids[key],metric['usable']])
            for field in _RULE_VALUES:number(metric[field])
        grain={key:row[key] for key in _GRAIN}
        records.append([row['vector_id'],row['start_time'],row['end_time'],cells,_copy(row['excluded_metrics']),inputs,
            _copy(row['quality']),_copy(row['references']),row['references_truncated'],row['event_count'],None if _same(base,grain) else _copy(grain)])
    return _Page([base,features,rules,records,extras],markers,floats,integers)


def _expand(page):
    base,features,rules,records,extra_values=page.metadata
    markers=iter(page.markers);floats=iter(page.floats);integers=iter(page.integers);extras=iter(extra_values)
    def number():
        marker=next(markers)
        if marker==0:return None
        if marker==1:return next(floats)
        if marker==2:return next(integers)
        if marker==3:return _copy(next(extras))
        if marker in (4,5):return marker==5
        raise ValueError("invalid vector numeric type")
    for identifier,start,end,cells,excluded,inputs,quality,refs,truncated,event_count,grain in records:
        vector=_copy(base if grain is None else grain)
        vector.update(vector_id=identifier,start_time=start,end_time=end,
            features={features[index][0]:dict(value=number(),raw_value=number(),**features[index][1]) for index in cells},
            excluded_metrics=_copy(excluded),
            rule_inputs={rules[index][0]:dict(zip(_RULE_VALUES,(number() for _ in _RULE_VALUES)))|dict(rules[index][1],usable=usable) for index,usable in inputs},
            quality=_copy(quality),references=_copy(refs),references_truncated=truncated,event_count=event_count)
        yield vector


def _encode(page):
    # Cached numeric arrays are native endian and remain unchanged on retry.
    floats,integers=page.floats,page.integers
    if sys.byteorder!='little':
        floats=floats[:];integers=integers[:];floats.byteswap();integers.byteswap()
    numeric=_NUMBERS.pack(len(page.markers),len(floats),len(integers))+page.markers+floats.tobytes()+integers.tobytes()
    if len(numeric)>MAX_BYTES:raise ValueError("vector numeric batch exceeds limit")
    numeric=zlib.compress(numeric,1);strings={}
    def intern(value):
        if value not in strings:strings[value]=len(strings)+1
        return strings[value]
    payload=pack_delta(page.metadata,intern)
    names=bytearray()
    for name in strings:
        encoded=name.encode("utf-8");names.extend(_LENGTH.pack(len(encoded)));names.extend(encoded)
    names=zlib.compress(names,1)
    if len(names)>MAX_BYTES:raise ValueError("vector batch dictionary exceeds limit")
    return _HEADER.pack(len(names),len(payload),len(numeric))+names+payload+numeric


def _inflate(blob):
    decoder=zlib.decompressobj()
    try:raw=decoder.decompress(blob,MAX_BYTES+1)
    except zlib.error as error:raise ValueError("invalid vector batch compression") from error
    if len(raw)>MAX_BYTES or not decoder.eof or decoder.unused_data:raise ValueError("invalid vector batch compression")
    return raw


def _decode(stream):
    header=stream.read(_HEADER.size)
    if not header:return None
    if len(header)!=_HEADER.size:raise ValueError("truncated vector batch header")
    dictionary_size,payload_size,numeric_size=_HEADER.unpack(header)
    if max(dictionary_size,payload_size,numeric_size)>MAX_BYTES:raise ValueError("invalid vector batch size")
    names=stream.read(dictionary_size);payload=stream.read(payload_size);numeric=stream.read(numeric_size)
    if len(names)!=dictionary_size or len(payload)!=payload_size or len(numeric)!=numeric_size:raise ValueError("truncated vector batch")
    raw=_inflate(names);strings={};offset=0
    while offset<len(raw):
        if offset+4>len(raw):raise ValueError("truncated vector batch string")
        size=_LENGTH.unpack_from(raw,offset)[0];offset+=4
        if offset+size>len(raw):raise ValueError("truncated vector batch string")
        strings[len(strings)+1]=raw[offset:offset+size].decode("utf-8");offset+=size
    page=unpack_delta(payload,strings)
    if not isinstance(page,list) or len(page)!=5:raise ValueError("invalid vector batch rows")
    raw=_inflate(numeric)
    if len(raw)<_NUMBERS.size:raise ValueError("truncated vector numeric batch")
    count,float_count,int_count=_NUMBERS.unpack_from(raw)
    if _NUMBERS.size+count+8*(float_count+int_count)!=len(raw):raise ValueError("invalid vector numeric batch size")
    offset=_NUMBERS.size;markers=bytearray(raw[offset:offset+count]);offset+=count
    floats=array('d');floats.frombytes(raw[offset:offset+8*float_count]);offset+=8*float_count
    integers=array('q');integers.frombytes(raw[offset:])
    if sys.byteorder!='little':floats.byteswap();integers.byteswap()
    try:expected=sum(2*len(record[3])+len(_RULE_VALUES)*len(record[5]) for record in page[3])
    except (IndexError,TypeError) as error:raise ValueError("invalid vector batch rows") from error
    if count!=expected:raise ValueError("invalid vector numeric value count")
    if (markers.count(1)!=float_count or markers.count(2)!=int_count or markers.count(3)!=len(page[4]) or
        sum(markers.count(marker) for marker in range(6))!=count):
        raise ValueError("invalid vector numeric types")
    return _Page(page,markers,floats,integers)



class VectorReplay:
    """Capture in original order; spill when the retained-object budget fills.

    Shared descriptors and typed numeric buffers skip serialization while
    their measured heap fits the cache budget. On overflow they move to typed
    spill pages, which are expanded one page at a time.
    """
    def __init__(self,path,*,page_size=64,memory_limit=32*1024*1024,page_bytes=8*1024*1024):
        if any(type(v) is not int or v<1 for v in (page_size,memory_limit,page_bytes)):
            raise ValueError("positive vector replay limits required")
        self.path=Path(path);self.page_size=page_size;self.memory_limit=memory_limit;self.page_bytes=page_bytes
        self.retained_bytes=0;self.pages=[];self._stream=None;self._complete=False;self._started=False

    def __enter__(self):return self
    def __exit__(self,*_):self.close()

    def close(self):
        if self._stream is not None:self._stream.close();self._stream=None
        self.pages.clear();self.retained_bytes=0
        if self.path.exists():self.path.unlink()

    def _spill_page(self,page):
        try:
            encoded=_encode(page)
            if len(encoded)>self.page_bytes:raise ValueError("vector replay page exceeds byte limit")
        except ValueError as error:
            if 'too large' not in str(error) and 'exceeds' not in str(error):raise
            # The encoder leaves retained pages intact for uncommon large
            # strings/numerous anchors that require smaller typed spill pages.
            rows=list(_expand(page))
            if len(rows)==1:raise ValueError("vector exceeds replay page byte limit") from error
            split=len(rows)//2
            self._spill_page(_compact(rows[:split]));self._spill_page(_compact(rows[split:]));return
        self._stream.write(encoded)

    def _save(self,rows):
        if not rows:return
        page=_compact(rows);size=_page_size(page)
        if self._stream is None and size<=self.page_bytes and self.retained_bytes+size<=self.memory_limit:
            self.pages.append(page);self.retained_bytes+=size;return
        if self._stream is None:
            self._stream=self.path.open("wb")
            for saved in self.pages:self._spill_page(saved)
            self.pages.clear();self.retained_bytes=0
        self._spill_page(page)

    def capture(self,rows):
        if self._started:raise ValueError("vector replay may capture only once")
        self._started=True;page=[]
        for row in rows:
            page.append(row)
            yield row
            if len(page)==self.page_size:
                self._save(page);page=[]
        self._save(page)
        if self._stream is not None:self._stream.flush()
        self._complete=True

    def rows(self):
        if not self._complete:raise ValueError("vector capture must finish before replay")
        if self._stream is None:
            for page in self.pages:yield from _expand(page)
        else:
            with self.path.open("rb") as source:
                while True:
                    page=_decode(source)
                    if page is None:break
                    yield from _expand(page)

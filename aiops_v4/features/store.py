"""Versioned canonical windows: dictionaries, float64 statistics and anchor links."""
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import zlib

from aiops_v4.storage import MAGIC, MAX_PAYLOAD, db_dump, load_json
from aiops_v4.data.position import record_at

INTS = ('count','numeric_count','missing_count','non_numeric_count','nonfinite_count','zero_count',
        'quantile_sample_count','quantile_capacity','distinct_count_lower_bound','input_count',
        'missing_input_count','invalid_input_count','finite_input_count','used_sample_count',
        'duplicate_input_count','conflicting_time_count','change_count','rate_sample_count',
        'counter_decrease_count','counter_gap_count','counter_first_sample_count')
FLOATS = ('min','max','mean','std','median','mad','p01','p05','q25','q75','p95','p99',
          'missing_fraction','first','last','slope_per_second','sum','delta_sum','rate_mean','rate_min','rate_max')
BOOLS = ('quantiles_exact','distinct_exact')
FIELDS = INTS + FLOATS + BOOLS
FIELD_NAMES = frozenset(FIELDS)
LAYOUT = struct.Struct('<BQQ'+'q'*len(INTS)+'d'*len(FLOATS)+'B'*len(BOOLS))
ANCHOR = struct.Struct('<5Q')
EPOCH = datetime(1970,1,1,tzinfo=timezone.utc)
_MISSING=object()
_DOUBLE=struct.Struct('<d')
_INT_FIELDS=tuple((key,1<<index) for index,key in enumerate(INTS))
_FLOAT_FIELDS=tuple((key,1<<index) for index,key in enumerate(FLOATS,len(INTS)))
_BOOL_FIELDS=tuple((key,1<<index) for index,key in enumerate(BOOLS,len(INTS)+len(FLOATS)))


def micros(stamp):
    delta=datetime.fromisoformat(stamp.replace('Z','+00:00'))-EPOCH
    return (delta.days*86400+delta.seconds)*1000000+delta.microseconds


def timestamp(value):
    return (EPOCH+timedelta(microseconds=value)).isoformat(timespec='microseconds').replace('+00:00','Z')


def _pack_uint(value):
    result=bytearray()
    while value>=128:
        result.append((value&127)|128);value>>=7
    result.append(value)
    return result


def _unpack_uint(blob,offset):
    result=0
    for shift in range(0,70,7):
        if offset>=len(blob):raise ValueError('truncated statistics integer')
        value=blob[offset];offset+=1;result|=(value&127)<<shift
        if not value&128:
            if result>=2**64:raise ValueError('invalid statistics integer')
            return result,offset
    raise ValueError('invalid statistics integer')


def pack_statistics(value):
    """Lossless repeated-value layout; IEEE754 byte keys keep signed zero.

    Presence and null masks distinguish missing fields from explicit None.
    Int64 uses signed varints; equal floating bit patterns share a palette.
    Fixed layout remains a fallback if it is smaller for unusual statistics.
    """
    present=nonnull=0;integer=bytearray()
    for key,mask in _INT_FIELDS:
        item=value.get(key,_MISSING)
        if item is _MISSING:continue
        present|=mask
        if item is None:continue
        nonnull|=mask
        if not isinstance(item,int) or not -(2**63)<=item<2**63:
            raise ValueError('statistics integer is outside int64')
        number=(item<<1)^(item>>63)
        if number<128:integer.append(number)
        else:integer.extend(_pack_uint(number))
    palette=[];lookup={};indices=bytearray();double=_DOUBLE.pack
    for key,mask in _FLOAT_FIELDS:
        item=value.get(key,_MISSING)
        if item is _MISSING:continue
        present|=mask
        if item is None:continue
        nonnull|=mask
        try:encoded=double(item)
        except (TypeError,ValueError,OverflowError,struct.error) as error:
            raise ValueError('invalid statistics float') from error
        number=lookup.get(encoded)
        if number is None:number=len(palette);lookup[encoded]=number;palette.append(encoded)
        indices.append(number)
    boolean=0
    for index,(key,mask) in enumerate(_BOOL_FIELDS):
        item=value.get(key,_MISSING)
        if item is _MISSING:continue
        present|=mask
        if item is None:continue
        nonnull|=mask
        if item not in (False,True):raise ValueError('invalid statistics boolean')
        boolean|=bool(item)<<index
    metadata={key:item for key,item in value.items() if key not in FIELD_NAMES}
    payload=(struct.pack('<BQQ',2,present,nonnull)+integer+bytes((len(palette),))+
             b''.join(palette)+indices+bytes((boolean,)))
    if len(payload)<LAYOUT.size:return payload,metadata
    values=[0 if value.get(key) is None else value[key] for key in FIELDS]
    try:return LAYOUT.pack(1,present,nonnull,*values),metadata
    except struct.error as error:raise ValueError('invalid statistics numeric value') from error


_STATISTICS_CODES=('q',)*len(INTS)+('d',)*len(FLOATS)+('B',)*len(BOOLS)
_STATISTICS_MASKS=tuple((key,1<<index,key in BOOLS) for index,key in enumerate(FIELDS))
_FULL_STATISTICS_LAYOUT=(LAYOUT,_STATISTICS_MASKS)


@lru_cache(maxsize=64)
def _selected_statistics_layout(fields):
    if any(type(key) is not str or key not in FIELD_NAMES for key in fields):
        raise ValueError('unknown or invalid statistics field name')
    selected=frozenset(fields)
    # Padding consumes the same bytes as the stored format without creating
    # Python numeric values for fields this consumer does not need.
    format='<BQQ'+''.join(code if key in selected else ('x' if code=='B' else '8x')
                         for key,code in zip(FIELDS,_STATISTICS_CODES))
    layout=struct.Struct(format)
    return layout,tuple(item for item in _STATISTICS_MASKS if item[0] in selected)


def _statistics_layout(fields):
    if fields is None:return _FULL_STATISTICS_LAYOUT
    if not isinstance(fields,(tuple,list)):
        raise ValueError('statistics_fields must be a tuple or list of field names')
    try:return _selected_statistics_layout(tuple(fields))
    except TypeError as error:
        raise ValueError('invalid statistics field name') from error


@lru_cache(maxsize=256)
def _compact_statistics_plan(present,nonnull,fields):
    selected={key for key,_,_ in fields}
    nulls={key:None for key,mask,_ in fields if present&mask and not nonnull&mask}
    integer_names=tuple(key for key,mask in _INT_FIELDS if nonnull&mask)
    integers=tuple(key if key in selected else None for key in integer_names)
    integer_projection=tuple((key,index) for index,key in enumerate(integer_names) if key in selected)
    capacity_index=integer_names.index('quantile_capacity') if 'quantile_capacity' in integer_names else None
    floats=tuple(key for key,mask in _FLOAT_FIELDS if nonnull&mask)
    projected=tuple((key,index) for index,key in enumerate(floats) if key in selected)
    booleans=tuple((key,1<<index) for index,(key,mask) in enumerate(_BOOL_FIELDS) if nonnull&mask and key in selected)
    return nulls,integers,projected,len(floats),booleans,integer_projection,capacity_index


_PALETTE_LAYOUTS=tuple(struct.Struct('<'+'d'*count) for count in range(len(FLOATS)+1))


def _unpack_statistics(blob,metadata,selection):
    if not isinstance(blob,bytes) or len(blob)<17:
        raise ValueError('truncated statistics payload')
    layout,fields=selection
    version,present,nonnull=struct.unpack_from('<BQQ',blob)
    if present>>len(FIELDS) or nonnull&~present:
        raise ValueError('invalid statistics field mask')
    result=dict(metadata)
    if version==1:
        values=layout.unpack(blob)
        for offset,(key,mask,is_boolean) in enumerate(fields,3):
            if present&mask:
                value=values[offset]
                result[key]=(bool(value) if is_boolean else value) if nonnull&mask else None
        return result
    if version!=2:raise ValueError('unsupported statistics layout')
    nulls,integers,floats,float_count,booleans,integer_projection,capacity_index=_compact_statistics_plan(present,nonnull,fields)
    result.update(nulls);offset=17
    short=blob[offset:offset+len(integers)]
    if len(short)==len(integers) and max(short,default=0)<128:
        for key,index in integer_projection:
            value=short[index];result[key]=(value>>1)^-(value&1)
        offset+=len(integers)
    else:
        # The usual report has capacity=128 and all other counts below64.
        # Check every encoded byte before using this fixed-position fast path.
        dense=blob[offset:offset+len(integers)+1]
        capacity=(capacity_index is not None and len(dense)==len(integers)+1 and
                  dense[capacity_index:capacity_index+2]==b'\x80\x02' and
                  max(dense[:capacity_index],default=0)<128 and
                  max(dense[capacity_index+2:],default=0)<128)
        if capacity:
            for key,index in integer_projection:
                if index==capacity_index:result[key]=128
                else:
                    value=dense[index+(index>capacity_index)];result[key]=(value>>1)^-(value&1)
            offset+=len(integers)+1
        else:
            for key in integers:
                if offset>=len(blob):raise ValueError('truncated statistics integer')
                value=blob[offset]
                if value&128:value,offset=_unpack_uint(blob,offset)
                else:offset+=1
                if key is not None:result[key]=(value>>1)^-(value&1)
    if offset>=len(blob):raise ValueError('truncated statistics palette')
    count=blob[offset];offset+=1
    if count>len(FLOATS) or offset+count*8+float_count+1!=len(blob):
        raise ValueError('invalid statistics palette size')
    palette=_PALETTE_LAYOUTS[count].unpack_from(blob,offset);offset+=count*8
    indices=blob[offset:offset+float_count];offset+=float_count
    if indices and max(indices)>=count:raise ValueError('invalid statistics palette index')
    for key,index in floats:result[key]=palette[indices[index]]
    boolean=blob[offset]
    if boolean>>len(BOOLS):raise ValueError('invalid statistics boolean payload')
    for key,mask in booleans:result[key]=bool(boolean&mask)
    return result


def validate_statistics(blob):
    """Validate transferred numbers without constructing a metric dictionary.

    New aggregation reports represent overflow with None plus metadata; active
    floating values must therefore be finite. Legacy reading remains lossless.
    """
    if not isinstance(blob,bytes) or not 17<=len(blob)<=LAYOUT.size:
        raise ValueError('invalid statistics payload size')
    version,present,nonnull=struct.unpack_from('<BQQ',blob)
    if present>>len(FIELDS) or nonnull&~present:
        raise ValueError('invalid statistics field mask')
    def finite(offset):
        # IEEE754 all-ones exponent encodes both Infinity and NaN, independent
        # of sign or mantissa. Read bytes rather than allocate Python floats.
        if blob[offset+7]&127==127 and blob[offset+6]&240==240:
            raise ValueError('nonfinite prepared statistics number')
    if version==1:
        if len(blob)!=LAYOUT.size:raise ValueError('invalid fixed statistics size')
        if max(blob[-len(BOOLS):])>1:raise ValueError('invalid statistics boolean value')
        for index,(_,mask) in enumerate(_FLOAT_FIELDS):
            if nonnull&mask:finite(17+8*(len(INTS)+index))
        return
    if version!=2:raise ValueError('unsupported statistics layout')
    integer_mask=nonnull&((1<<len(INTS))-1)
    integer_count=integer_mask.bit_count();offset=17
    short=blob[offset:offset+integer_count]
    if len(short)==integer_count and max(short,default=0)<128:
        offset+=integer_count
    else:
        capacity_bit=1<<INTS.index('quantile_capacity')
        capacity_index=(integer_mask&(capacity_bit-1)).bit_count()
        dense=blob[offset:offset+integer_count+1]
        capacity=(integer_mask&capacity_bit and len(dense)==integer_count+1 and
                  dense[capacity_index:capacity_index+2]==b'\x80\x02' and
                  max(dense[:capacity_index],default=0)<128 and
                  max(dense[capacity_index+2:],default=0)<128)
        if capacity:offset+=integer_count+1
        else:
            for _ in range(integer_count):
                if offset>=len(blob):raise ValueError('truncated statistics integer')
                if blob[offset]&128:_,offset=_unpack_uint(blob,offset)
                else:offset+=1
    if offset>=len(blob):raise ValueError('truncated statistics palette')
    count=blob[offset];offset+=1
    float_count=((nonnull>>len(INTS))&((1<<len(FLOATS))-1)).bit_count()
    if count>float_count or offset+count*8+float_count+1!=len(blob):
        raise ValueError('invalid statistics palette size')
    for index in range(count):finite(offset+index*8)
    offset+=count*8
    indices=blob[offset:offset+float_count];offset+=float_count
    if indices and max(indices)>=count:raise ValueError('invalid statistics palette index')
    boolean=blob[offset]
    allowed=(nonnull>>(len(INTS)+len(FLOATS)))&((1<<len(BOOLS))-1)
    if boolean&~allowed:raise ValueError('invalid statistics boolean payload')


def unpack_statistics(blob,metadata,*,statistics_fields=None):
    """Decode all fields by default, or explicitly project known numeric fields.

    Unknown metadata is retained regardless of the projection. Requested field
    names must be a tuple/list drawn from FIELDS; absent fields remain absent.
    """
    return _unpack_statistics(blob,metadata,_statistics_layout(statistics_fields))


def configure_write_connection(db, *, memory_bytes=None, workers=1, cache_bytes=None):
    """Apply one bounded, durable policy to new and reopened writer connections.

    Call before starting a transaction: SQLite otherwise ignores foreign_keys.
    Journal mode is retained (new connections default to DELETE); synchronous
    FULL keeps a group and its progress marker recoverable after a crash.
    Returns the configured cache budget in bytes. Each connection needs its own
    budget when a caller opens multiple connections in the same process.
    """
    if type(workers) is not int or workers < 1:
        raise ValueError('positive worker count required')
    if memory_bytes is not None and (type(memory_bytes) is not int or memory_bytes < 1):
        raise ValueError('positive memory budget required')
    if cache_bytes is not None and (type(cache_bytes) is not int or cache_bytes < 1024):
        raise ValueError('cache budget must be at least one KiB')
    if db.in_transaction:
        raise ValueError('configure write connection before starting a transaction')
    journal=db.execute('PRAGMA journal_mode').fetchone()[0]
    file_backed=any(name=='main' and path for _,name,path in db.execute('PRAGMA database_list'))
    if journal=='off' or (file_backed and journal=='memory'):
        raise ValueError('durable writes require a disk-backed rollback journal or WAL')
    if cache_bytes is None:
        cache_bytes = (64*1024**2 if memory_bytes is None else
                       min(512*1024**2, max(8*1024**2, memory_bytes//(workers+2)//4)))
    cache_kib = max(1, cache_bytes//1024)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA temp_store=FILE')
    db.execute(f'PRAGMA cache_size=-{cache_kib}')
    return cache_kib*1024


def configure_build_connection(db, *, memory_bytes=None, workers=1, cache_bytes=None):
    """Use durable sequential WAL writes while building a private target store.

    Call outside a transaction for both new and resumed targets. The WAL and
    shared-memory sidecars are part of a checkpoint until finalization; retain
    them on interruption. Generic writer connections keep their current mode.
    """
    budget=configure_write_connection(db,memory_bytes=memory_bytes,workers=workers,cache_bytes=cache_bytes)
    mode=db.execute('PRAGMA journal_mode=WAL').fetchone()[0]
    if mode!='wal':raise RuntimeError('WAL build journal mode is unavailable')
    db.execute('PRAGMA wal_autocheckpoint=1000')
    return budget


def finalize_build_connection(db):
    """Materialize all committed WAL frames before caller closes and publishes.

    The caller must commit its final progress/metadata first, then call this,
    close the connection, and only afterward rename or seal the database.
    A busy checkpoint refuses publication and preserves recoverable sidecars.
    """
    if db.in_transaction:
        raise ValueError('finalize build connection after committing its transaction')
    checkpoint=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    if checkpoint[0]!=0:
        raise RuntimeError('build WAL checkpoint is blocked; database is not ready for publication')
    mode=db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
    if mode!='delete':raise RuntimeError('build database is not ready for DELETE-mode publication')


def create_store(path, *, memory_bytes=None, workers=1, cache_bytes=None):
    db=sqlite3.connect(path)
    configure_write_connection(db, memory_bytes=memory_bytes, workers=workers, cache_bytes=cache_bytes)
    db.executescript('''PRAGMA user_version=4;
      CREATE TABLE metadata(key TEXT PRIMARY KEY,payload BLOB NOT NULL);
      CREATE TABLE groups(id INTEGER PRIMARY KEY,series TEXT UNIQUE,state_group TEXT UNIQUE,entity TEXT,source TEXT,payload BLOB NOT NULL);
      CREATE TABLE definitions(id INTEGER PRIMARY KEY,payload BLOB NOT NULL);
      CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT UNIQUE,payload BLOB NOT NULL);
      CREATE TABLE flags(id INTEGER PRIMARY KEY,payload BLOB NOT NULL);
      CREATE TABLE extras(id INTEGER PRIMARY KEY,payload BLOB NOT NULL);
      CREATE TABLE windows(id BLOB NOT NULL UNIQUE,grp INTEGER REFERENCES groups(id),start INTEGER,end INTEGER,
        records INTEGER,observed INTEGER,truncated INTEGER,flags INTEGER REFERENCES flags(id),statistics BLOB NOT NULL,extras BLOB NOT NULL,seq INTEGER PRIMARY KEY);
      CREATE TABLE feature_blocks(id INTEGER PRIMARY KEY,grp INTEGER REFERENCES groups(id),windows INTEGER NOT NULL,raw_size INTEGER NOT NULL,payload BLOB NOT NULL);
      CREATE TABLE anchors(id INTEGER PRIMARY KEY,digest BLOB UNIQUE,file INTEGER REFERENCES files(id),payload BLOB NOT NULL);
      CREATE TABLE _window_refs(window INTEGER REFERENCES windows(seq),position INTEGER,anchor INTEGER REFERENCES anchors(id),PRIMARY KEY(window,position)) WITHOUT ROWID;
      CREATE VIEW window_refs AS SELECT w.id AS window,r.position,r.anchor FROM _window_refs r JOIN windows w ON w.seq=r.window;
      CREATE TABLE progress(grp TEXT PRIMARY KEY);
    ''')
    return db


class StoreWriter:
    def __init__(self, db, batch, files, *, anchor_cache_size=65536):
        self.db,self.batch=db,batch
        self.groups={};self.definitions={};self.flags={};self.extras={};self.file_ids={}
        self._initialize_caches(anchor_cache_size)
        rows=[]
        for i,f in enumerate(files,1):
            rows.append((i,f['path'],db_dump(f)));self.file_ids[f['path']]=i
        db.executemany('INSERT INTO files VALUES (?,?,?)',rows)

    def _initialize_caches(self, anchor_cache_size=65536):
        if type(anchor_cache_size) is not int or anchor_cache_size < 1:
            raise ValueError('positive anchor cache size required')
        self.anchor_cache_size=anchor_cache_size
        self.anchor_ids=OrderedDict()
        self._next_window=self.db.execute('SELECT coalesce(max(seq),0)+1 FROM windows').fetchone()[0]
        self._next_group=max(self.groups.values(),default=0)+1
        self._next_ids={name:max(getattr(self,name).values(),default=0)+1
                        for name in ('definitions','flags','extras')}

    def _reload_caches(self):
        self.file_ids={p:i for i,p in self.db.execute('SELECT id,path FROM files')}
        self.groups={s:i for i,s in self.db.execute('SELECT id,series FROM groups')}
        for table in ('definitions','flags','extras'):
            setattr(self,table,{self._key(load_json(v)):i for i,v in
                               self.db.execute(f'SELECT id,payload FROM {table}')})
        self._initialize_caches(getattr(self,'anchor_cache_size',65536))

    @classmethod
    def resume(cls, db, batch, *, anchor_cache_size=65536):
        """Restore dictionary IDs from a committed version-four checkpoint."""
        if db.execute('PRAGMA user_version').fetchone()[0] != 4:
            raise ValueError('only version-four feature stores support resumed writes; old unsealed stores require a rebuild')
        writer=cls.__new__(cls)
        writer.db,writer.batch=db,batch
        writer.anchor_cache_size=anchor_cache_size
        writer._reload_caches()
        return writer

    @staticmethod
    def _key(value):
        return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)

    def _intern(self,table,cache,value):
        key=self._key(value)
        if key not in cache:
            # The interning key is already canonical JSON. Avoid serializing it
            # again through db_dump for each new definition/flags/extra value.
            encoded=key.encode('utf-8')
            if len(encoded)>MAX_PAYLOAD:raise ValueError('JSON payload exceeds storage limit')
            ident=self._next_ids[table]
            self.db.execute(f'INSERT INTO {table} VALUES (?,?)',(ident,MAGIC+zlib.compress(encoded,1)))
            self._next_ids[table]=ident+1;cache[key]=ident
        return cache[key]

    def _prepare(self,window,anchors):
        from aiops_v4.states.matrix import window_vector
        from .write_codec import GRAIN_FIELDS,series_key,window_extras
        from .block_codec import fixed_statistics
        grain={key:window[key] for key in GRAIN_FIELDS}
        series=series_key(window,self.batch)
        if series not in self.groups:
            group=self._next_group
            state_group=window_vector(window,self.batch)['group_id']
            self.db.execute('INSERT INTO groups VALUES (?,?,?,?,?,?)',(group,series,state_group,window['identity']['entity_id'],window['source'],db_dump(grain)))
            self._next_group=group+1;self.groups[series]=group
        group=self.groups[series];pieces=[]
        for name,metric in window['metrics'].items():
            numeric,extra=fixed_statistics(metric)
            definition=self._intern('definitions',self.definitions,dict(name=name,extra=extra))
            pieces.append((definition,numeric))
        flags=self._intern('flags',self.flags,window['quality_flags'])
        identifier=bytes.fromhex(window['window_id'])
        if len(identifier)!=32:raise ValueError('invalid feature content digest')
        sequence=self._next_window;self._next_window+=1
        row=(identifier,group,micros(window['start_time']),micros(window['end_time']),window['record_count'],
             window['coverage']['observed_timestamps'],window['references_truncated'],flags,pieces,
             self._intern('extras',self.extras,window_extras(window)),sequence)
        links=[]
        for position,ref in enumerate(window['references']):
            digest=bytes.fromhex(ref['record_id'])
            if len(digest)!=32:raise ValueError('invalid feature reference digest')
            anchor=anchors[ref['record_id']]
            links.append((sequence,position,digest,self.file_ids[ref['source_file']],ANCHOR.pack(*(anchor[key] for key in ('record_index','line_start','line_end','byte_start','byte_length')))))
        return row,links

    def _flush(self,windows,refs):
        if not windows:return
        from .block_codec import MAX_WINDOWS,MAX_BLOCK_BYTES,TARGET_BLOCK_ENTRIES,dense_size,encode_block,pack_storage,pack_token,TOKEN_MAGIC
        pending=[];entries=0;group=None
        def block():
            if not pending:return
            raw=encode_block([row[8] for row in pending])
            identifier=self.db.execute('INSERT INTO feature_blocks(grp,windows,raw_size,payload) VALUES (?,?,?,?)',
                (pending[0][1],len(pending),len(raw),pack_storage(raw))).lastrowid
            self.db.executemany('INSERT INTO windows VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (row[:8]+(pack_token(identifier,index),)+row[9:] for index,row in enumerate(pending)))
        for row in windows:
            if isinstance(row[8],bytes) and row[8].startswith(TOKEN_MAGIC):
                block();pending=[];entries=0;group=None
                self.db.execute('INSERT INTO windows VALUES (?,?,?,?,?,?,?,?,?,?,?)',row)
                continue
            count=len(row[8])
            if pending and (row[1]!=group or len(pending)==MAX_WINDOWS or entries+count>TARGET_BLOCK_ENTRIES or dense_size(len(pending)+1,entries+count)>MAX_BLOCK_BYTES):
                block();pending=[];entries=0
            if dense_size(1,count)>MAX_BLOCK_BYTES:raise ValueError('feature window exceeds block size limit')
            group=row[1];pending.append(row);entries+=count
        block()
        ids={};missing={}
        for _,_,digest,file,payload in refs:
            if digest in ids or digest in missing:continue
            if digest in self.anchor_ids:
                ids[digest]=self.anchor_ids[digest];self.anchor_ids.move_to_end(digest)
            else:
                missing[digest]=(digest,file,payload)
        if missing:
            self.db.executemany('INSERT OR IGNORE INTO anchors(digest,file,payload) VALUES (?,?,?)',missing.values())
            digests=list(missing)
            # 900 parameters also works with older SQLite's 999-variable limit.
            for start in range(0,len(digests),900):
                page=digests[start:start+900];marks=','.join('?' for _ in page)
                for digest,ident in self.db.execute(f'SELECT digest,id FROM anchors WHERE digest IN ({marks})',page):
                    ids[digest]=ident;self.anchor_ids[digest]=ident
                    if len(self.anchor_ids)>self.anchor_cache_size:self.anchor_ids.popitem(last=False)
        self.db.executemany('INSERT INTO _window_refs VALUES (?,?,?)',
                            ((window,position,ids[digest]) for window,position,digest,_,_ in refs))

    def add_many(self, rows, *, batch_size=512):
        """Write bounded batches of (window, anchors), leaving commit to caller.

        No chunk commits internally. Windows, raw references and the caller's
        progress marker therefore share the same crash-recovery boundary. A
        failing iterable or SQL statement rolls back this entire call and
        restores the in-memory dictionary/anchor caches before propagating.
        """
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError('positive write batch size required')
        # Keep compatibility with the historical __new__ resume initialization.
        if not hasattr(self,'anchor_ids'):self._initialize_caches()
        if not self.db.in_transaction:self.db.execute('BEGIN')
        self.db.execute('SAVEPOINT store_writer_batch')
        try:
            from .block_codec import dense_size,MAX_BLOCK_BYTES
            windows=[];refs=[];entries=0
            for window,anchors in rows:
                row,links=self._prepare(window,anchors)
                if windows and dense_size(len(windows)+1,entries+len(row[8]))>MAX_BLOCK_BYTES:
                    self._flush(windows,refs);windows=[];refs=[];entries=0
                windows.append(row);refs.extend(links);entries+=len(row[8])
                if len(windows)==min(batch_size,256):
                    self._flush(windows,refs);windows=[];refs=[];entries=0
            self._flush(windows,refs)
            self.db.execute('RELEASE store_writer_batch')
        except BaseException:
            # Some SQLite failures roll back the whole transaction themselves.
            # Preserve their original error rather than masking it with a
            # missing-savepoint exception during cleanup.
            if self.db.in_transaction:
                self.db.execute('ROLLBACK TO store_writer_batch')
                self.db.execute('RELEASE store_writer_batch')
            self._reload_caches()
            raise

    def import_prepared(self, source, *, batch_size=512, check=None):
        """Batch-import typed worker output within the caller's group transaction.

        Dictionary JSON is decoded once per local ID. Numeric statistics and
        raw anchors remain packed; the worker's bounded dictionaries never
        require complete window dictionaries in the parent process.
        """
        from .write_codec import iter_metrics,unpack_references,validate_group_grain
        from .block_codec import NumericBlock,unpack_storage,pack_storage,unpack_token,pack_token,TOKEN_MAGIC,fixed_statistics,dense_size,MAX_BLOCK_BYTES
        if type(batch_size) is not int or batch_size<1:
            raise ValueError('positive write batch size required')
        source_version=source.execute('PRAGMA user_version').fetchone()[0]
        if source_version not in (3,4):
            raise ValueError('unsupported prepared feature format')
        imported_blocks=OrderedDict()
        mappings={table:OrderedDict() for table in ('groups','definitions','flags','extras')}
        files={identifier:self.file_ids[path] for identifier,path in source.execute('SELECT id,path FROM files')}
        def mapped(table,identifier):
            cache=mappings[table]
            if identifier in cache:return cache[identifier]
            if table=='groups':
                saved=source.execute('SELECT key,state_group,entity,source,payload FROM groups WHERE id=?',(identifier,)).fetchone()
                if saved is None:raise ValueError('unknown prepared feature group')
                series,state_group,entity,kind,payload=saved
                validate_group_grain(payload,self.batch,series,state_group,entity,kind)
                result=self.groups.get(series)
                if result is None:
                    result=self._next_group
                    self.db.execute('INSERT INTO groups VALUES (?,?,?,?,?,?)',(result,series,state_group,entity,kind,payload))
                    self._next_group+=1;self.groups[series]=result
            else:
                saved=source.execute(f'SELECT payload FROM {table} WHERE id=?',(identifier,)).fetchone()
                if saved is None:raise ValueError('unknown prepared feature dictionary')
                result=self._intern(table,getattr(self,table),load_json(saved[0]))
            cache[identifier]=result
            if len(cache)>2048:cache.popitem(last=False)
            return result
        if not self.db.in_transaction:self.db.execute('BEGIN')
        self.db.execute('SAVEPOINT store_writer_batch')
        try:
            windows=[];refs=[];entries=0
            for number,row in enumerate(source.execute(
                    'SELECT id,grp,start,end,records,observed,truncated,flags,statistics,extras,refs FROM windows ORDER BY position'),1):
                if check is not None and number%1000==0:check()
                identifier,group,start,end,count,observed,truncated,flags,statistics,extra,references=row
                if not isinstance(identifier,bytes) or len(identifier)!=32:
                    raise ValueError('invalid prepared feature digest')
                sequence=self._next_window;self._next_window+=1
                native_group=mapped('groups',group)
                if source_version==4 and statistics.startswith(TOKEN_MAGIC):
                    local_block,offset=unpack_token(statistics)
                    cached=imported_blocks.get(local_block)
                    if cached is None:
                        saved=source.execute('SELECT grp,windows,raw_size,payload FROM feature_blocks WHERE id=?',(local_block,)).fetchone()
                        if saved is None:raise ValueError('prepared feature block was not flushed')
                        local_group,block_count,raw_size,payload=saved
                        block=NumericBlock(unpack_storage(payload,expected_size=raw_size))
                        if block.count!=block_count or local_group!=group:raise ValueError('invalid prepared feature block group/count')
                        block.validate_finite()
                        raw=block.remap(lambda definition:mapped('definitions',definition))
                        native_block=self.db.execute('INSERT INTO feature_blocks(grp,windows,raw_size,payload) VALUES (?,?,?,?)',
                            (native_group,block.count,len(raw),pack_storage(raw))).lastrowid
                        cached=(native_block,block.count,local_group);imported_blocks[local_block]=cached
                        if len(imported_blocks)>2:imported_blocks.popitem(last=False)
                    if not 0<=offset<cached[1] or cached[2]!=group:raise ValueError('invalid prepared block row/group')
                    numeric=pack_token(cached[0],offset)
                else:
                    numeric=[]
                    for definition,payload in iter_metrics(statistics):
                        validate_statistics(payload)
                        fixed=payload if payload[0]==1 else fixed_statistics(unpack_statistics(payload,{}))[0]
                        numeric.append((mapped('definitions',definition),fixed))
                numeric_count=len(numeric) if isinstance(numeric,list) else 0
                if windows and dense_size(len(windows)+1,entries+numeric_count)>MAX_BLOCK_BYTES:
                    self._flush(windows,refs);windows=[];refs=[];entries=0
                windows.append((identifier,native_group,start,end,count,observed,truncated,mapped('flags',flags),numeric,mapped('extras',extra),sequence))
                entries+=numeric_count
                for position,(file,digest,*positions) in enumerate(unpack_references(references)):
                    refs.append((sequence,position,digest,files[file],ANCHOR.pack(*positions)))
                if len(windows)==min(batch_size,256):
                    self._flush(windows,refs);windows=[];refs=[];entries=0
            self._flush(windows,refs)
            self.db.execute('RELEASE store_writer_batch')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK TO store_writer_batch')
                self.db.execute('RELEASE store_writer_batch')
            self._reload_caches()
            raise

    def add(self,window,anchors):
        self.add_many(((window,anchors),),batch_size=1)


class FeatureStore:
    def __init__(self,path,*,verify=True):
        self.path=Path(path).resolve(strict=True)
        if verify:
            from aiops_v4.data.cache import verify_layer
            verify_layer(self.path.parent)
        self.db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
        self.db.execute('PRAGMA query_only=ON');self.db.execute('PRAGMA cache_size=-8192')
        self.version=self.db.execute('PRAGMA user_version').fetchone()[0]
        if self.version not in (1,2,3,4): raise ValueError('unsupported feature format')
        self._block_cache=OrderedDict()
        self.summary=load_json(self.db.execute("SELECT payload FROM metadata WHERE key='summary'").fetchone()[0])
        self.groups={r[0]:load_json(r[1]) for r in self.db.execute('SELECT id,payload FROM groups')}
        self.definitions={r[0]:load_json(r[1]) for r in self.db.execute('SELECT id,payload FROM definitions')}
        self.flags={r[0]:load_json(r[1]) for r in self.db.execute('SELECT id,payload FROM flags')}
        self.extras={r[0]:load_json(r[1]) for r in self.db.execute('SELECT id,payload FROM extras')} if self.version>=2 else {}
        self.files={r[0]:load_json(r[1]) for r in self.db.execute('SELECT id,payload FROM files')}

    def decode_anchor(self,digest,file,payload):
        if self.version==1:return load_json(payload)
        fields=('record_index','line_start','line_end','byte_start','byte_length')
        return dict(record_id=digest.hex(),source_file=self.files[file]['path'],**dict(zip(fields,ANCHOR.unpack(payload))))

    def __enter__(self):return self
    def __exit__(self,*_):self.db.close()

    def _decode(self,row,refs,*,statistics_fields=None):
        selection=_statistics_layout(statistics_fields)
        ident,grp,start,end,count,observed,truncated,flags,blob,extra=row[:10]
        result=dict(self.groups[grp]);extras=dict(self.extras[extra]) if self.version>=2 else load_json(extra);coverage=dict(extras.pop('coverage'))
        coverage['observed_timestamps']=observed
        result.update(extras,window_id=ident.hex(),start_time=timestamp(start),end_time=timestamp(end),record_count=count,
                      coverage=coverage,quality_flags=self.flags[flags],references=refs,references_truncated=bool(truncated))
        from .write_codec import iter_metrics
        from .block_codec import TOKEN_MAGIC
        if blob.startswith(TOKEN_MAGIC):
            block,offset=self._resolve_block(blob,grp)
            metrics=block.decode(offset,self.definitions,selection)
        else:
            metrics={}
            for definition,numeric in iter_metrics(zlib.decompress(blob)):
                d=self.definitions[definition]
                metrics[d['name']]=_unpack_statistics(numeric,d['extra'],selection)
        result['metrics']=metrics
        return result

    def _resolve_block(self,token,group):
        from .block_codec import NumericBlock,unpack_token,unpack_storage
        identifier,offset=unpack_token(token)
        cached=self._block_cache.get(identifier)
        if cached is None:
            row=self.db.execute('SELECT grp,windows,raw_size,payload FROM feature_blocks WHERE id=?',(identifier,)).fetchone()
            if row is None:raise ValueError('unknown feature block')
            block_group,count,size,payload=row
            block=NumericBlock(unpack_storage(payload,expected_size=size))
            if block.count!=count:raise ValueError('invalid stored feature block window count')
            cached=(block_group,block);self._block_cache[identifier]=cached
            if len(self._block_cache)>2:self._block_cache.popitem(last=False)
        else:self._block_cache.move_to_end(identifier)
        block_group,block=cached
        if block_group!=group or not 0<=offset<block.count:raise ValueError('feature block row/group outside boundary')
        return block,offset

    def decode_rows(self,rows,*,include_references=True,statistics_fields=None):
        """Decode rows with optional reference and numeric statistics projection."""
        _statistics_layout(statistics_fields)
        if statistics_fields is not None:statistics_fields=tuple(statistics_fields)
        rows=list(rows)
        if not rows:return []
        identifiers=[r[0] for r in rows];refs={i:[] for i in identifiers}
        if not include_references:
            return [self._decode(r,[],statistics_fields=statistics_fields) for r in rows]
        # One query per page rather than one per window/reference.
        marks=','.join('?' for _ in identifiers)
        for window,digest,file,payload in self.db.execute(f'SELECT r.window,a.digest,a.file,a.payload FROM window_refs r JOIN anchors a ON a.id=r.anchor WHERE r.window IN ({marks}) ORDER BY r.window,r.position',identifiers):
            anchor=self.decode_anchor(digest,file,payload)
            refs[window].append({k:anchor[k] for k in ('record_id','source_file','record_index','line_start','line_end')})
        return [self._decode(r,refs[r[0]],statistics_fields=statistics_fields) for r in rows]

    def windows(self,group=None,*,page_size=256,include_references=True,statistics_fields=None):
        _statistics_layout(statistics_fields)
        if statistics_fields is not None:statistics_fields=tuple(statistics_fields)
        sql='SELECT w.* FROM windows w JOIN groups g ON g.id=w.grp';args=[]
        if group is not None:sql+=' WHERE g.state_group=?';args=[group]
        sql+=' ORDER BY '+('w.start,w.id' if group else "(g.source='netflow'),g.series,w.start,w.id")
        cursor=self.db.execute(sql,args)
        while True:
            rows=cursor.fetchmany(min(page_size,256))
            if not rows:break
            yield from self.decode_rows(rows,include_references=include_references,statistics_fields=statistics_fields)

    def get_windows(self,identifiers):
        values=[bytes.fromhex(i) for i in identifiers];results={}
        for start in range(0,len(values),256):
            page=values[start:start+256];marks=','.join('?' for _ in page)
            for row in self.decode_rows(self.db.execute(f'SELECT * FROM windows WHERE id IN ({marks})',page)):
                results[row['window_id']]=row
        return results

    def anchor(self,identifier):
        row=self.db.execute('SELECT digest,file,payload FROM anchors WHERE digest=?',(bytes.fromhex(identifier),)).fetchone()
        if row is None:raise ValueError('unknown/unregistered raw reference')
        return self.decode_anchor(*row),self.files[row[1]]

    def raw_record(self,identifier):
        anchor,manifest=self.anchor(identifier)
        return record_at(self.summary['input_root'],self.summary['batch'],manifest,anchor).to_dict()


def export_windows(path,destination):
    with FeatureStore(path) as store, Path(destination).open('x',encoding='utf-8') as out:
        for row in store.windows():out.write(json.dumps(row,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')

"""Compact typed NetFlow moments and anchors, with legacy JSON compatibility.

One baseline template and metric order are interned per series. The window
body is positional, uses native binary floats/integers, and compresses repeated
anchor text. Compact records do not format or parse JSON. Python marshal v2 is
used only for data values; decoded values are validated before consumption.
"""
from collections import OrderedDict
import marshal
import math
import struct
import sys
import zlib

from aiops_v4.storage import MAX_PAYLOAD, db_dump, load_json

MAGIC = b'V4F1'
_TEMPLATE_MAGIC = b'V4T1'
_COUNTS = ('inputs', 'missing', 'invalid', 'n', 'zeros')
_NUMBERS = ('mean', 'm2', 'total', 'min', 'max')
_POINTS = ('first', 'last')
_METRIC_FIELDS = frozenset((*_COUNTS, *_NUMBERS, *_POINTS))
_VALUE_FIELDS = frozenset(('template', 'count', 'flags', 'refs', 'metrics', 'last_time_seen'))
_REF_FIELDS = ('record_id', 'source_file', 'record_index', 'line_start', 'line_end',
               'byte_start', 'byte_length', '_cells')
_REF_SET = frozenset(_REF_FIELDS)
_DOUBLE = struct.Struct('<d')
_POSITIONS = struct.Struct('<5Q')
_REF_POSITIONS = ('record_index','line_start','line_end','byte_start','byte_length')
_SIGNED = struct.Struct('<i')
MAX_TYPED_NODES = 1 << 18
MAX_TYPED_DEPTH = 64


def _plain(value, depth=0):
    """Validate JSON-compatible data and canonicalize only metadata dictionaries."""
    if depth > 64:
        raise ValueError('flow nesting limit exceeded')
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('nonfinite flow number')
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item, depth + 1) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('invalid flow mapping key')
        return {key: _plain(value[key], depth + 1) for key in sorted(value)}
    raise ValueError('unsupported flow value')



def _validate(value, depth=0):
    if depth > 64:
        raise ValueError('flow nesting limit exceeded')
    kind = type(value)
    if value is None or kind in (bool, int, str):return
    if kind is float:
        if not math.isfinite(value):raise ValueError('nonfinite flow number')
        return
    if kind is dict:
        if any(type(key) is not str for key in value):raise ValueError('invalid flow mapping key')
        children = value.values()
    elif kind in (list, tuple):children = value
    else:raise ValueError('unsupported typed flow value')
    # The fixed body contains mostly primitive cells; avoid one Python function
    # call for every string/counter while still rejecting code and other types.
    for item in children:
        item_kind = type(item)
        if item is None or item_kind in (bool, int, str):continue
        if item_kind is float:
            if not math.isfinite(item):raise ValueError('nonfinite flow number')
        else:_validate(item, depth + 1)


def _copy(value):
    if type(value) is dict:return {key:_copy(item) for key,item in value.items()}
    if type(value) is list:return [_copy(item) for item in value]
    return value


def _binary(value):
    payload = marshal.dumps(value, 2)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError('flow payload exceeds storage limit')
    _preflight(payload)
    return payload



def _preflight(payload):
    """Bound every marshal-v2 allocation declaration before calling its C reader.

    Frames hold only a count (list/tuple) or key/value phase (dictionary), so
    checking does not materialize payload objects. No code/reference/interned
    tags are accepted. String/int digits must fit the remaining byte stream;
    complete streams are additionally bounded by depth and total value nodes.
    """
    offset = 0;length = len(payload);nodes = 0
    frames = [[1, False]]  # remaining values, dictionary expects a key
    while frames:
        frame = frames[-1]
        if frame[0] == 0:
            frames.pop();continue
        if offset >= length:raise ValueError('truncated typed flow payload')
        kind = payload[offset];offset += 1
        if frame[0] is None:
            if frame[1]:
                if kind == 48:  # TYPE_NULL ends a dictionary, only at a key.
                    frames.pop();continue
                if kind != 117:raise ValueError('invalid typed flow mapping key')
            frame[1] = not frame[1]
        else:frame[0] -= 1
        nodes += 1
        if nodes > MAX_TYPED_NODES:raise ValueError('typed flow node limit exceeded')
        if kind in (78, 70, 84):continue  # None, False, True
        if kind in (105, 103, 73):  # signed int32, binary float64, old int64
            size = 4 if kind == 105 else 8
            if size > length - offset:raise ValueError('truncated typed flow scalar')
            offset += size;continue
        if kind in (115, 117, 108, 91, 40):  # bytes, unicode, long, list, tuple
            if 4 > length - offset:raise ValueError('truncated typed flow length')
            count = _SIGNED.unpack_from(payload,offset)[0];offset += 4
            if kind == 108:
                size = abs(count) * 2
                if size > length - offset:raise ValueError('invalid typed flow integer digits')
                offset += size;continue
            if count < 0:raise ValueError('negative typed flow length')
            if kind in (115,117):
                if count > length - offset:raise ValueError('invalid typed flow text length')
                offset += count;continue
            if count > length - offset or count > MAX_TYPED_NODES - nodes:
                raise ValueError('invalid typed flow collection length')
            if len(frames) >= MAX_TYPED_DEPTH:raise ValueError('typed flow depth limit exceeded')
            frames.append([count,False]);continue
        if kind == 123:  # dict: pairs terminated by TYPE_NULL, no declared size
            if len(frames) >= MAX_TYPED_DEPTH:raise ValueError('typed flow depth limit exceeded')
            frames.append([None,True]);continue
        raise ValueError('unsupported typed flow marshal tag')
    if offset != length:raise ValueError('trailing typed flow payload')


def _unbinary(payload):
    if not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD:
        raise ValueError('invalid or oversized flow payload')
    _preflight(payload)
    try:
        value = marshal.loads(payload)
        # v2 has no reference flags: canonical reserialization rejects trailing
        # bytes/duplicate mapping entries without marshal.load's per-cell I/O.
        if marshal.dumps(value, 2) != payload:
            raise ValueError('noncanonical or trailing flow payload')
        return value
    except (EOFError, TypeError, OverflowError) as error:
        raise ValueError('invalid typed flow payload') from error


def _inflate(payload):
    try:
        decoder = zlib.decompressobj()
        data = decoder.decompress(payload, MAX_PAYLOAD + 1)
        if len(data) > MAX_PAYLOAD or not decoder.eof or decoder.unused_data:
            raise ValueError('invalid or oversized compressed flow payload')
        return data
    except zlib.error as error:
        raise ValueError('invalid compressed flow payload') from error


def _same(left, right):
    """Do not conflate int/float or positive/negative zero in template patches."""
    if left is right:return True
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if type(left) is float:
        return _DOUBLE.pack(left) == _DOUBLE.pack(right)
    return left == right


def _size(value, seen=None):
    seen = set() if seen is None else seen
    marker = id(value)
    if marker in seen:
        return 0
    seen.add(marker)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_size(key, seen) + _size(item, seen) for key, item in value.items())
    elif isinstance(value, (list, tuple)):
        size += sum(_size(item, seen) for item in value)
    return size


def _extras(value, fields):
    if value.keys() <= fields:return None
    return _plain({key: item for key, item in value.items() if key not in fields})



def _check_extras(value, reserved):
    if type(value) is not dict or value.keys() & reserved:
        raise ValueError('invalid reserved flow metadata')
    _validate(value)


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('invalid flow count')
    return value


class FlowCodec:
    """Encode new spools; old checkpoints without templates retain JSON writes.

    Registration and window writes use the caller's transaction. Decoded
    templates share a fixed byte budget and a 128-entry limit. The typed binary
    envelope is separate from legacy V4Z1 compressed JSON.
    """
    def __init__(self, db, create=False, cache_bytes=4 * 1024**2):
        if type(cache_bytes) is not int or cache_bytes < 0:
            raise ValueError('invalid flow template cache budget')
        self.db = db
        if create:
            db.execute('CREATE TABLE IF NOT EXISTS flow_templates('
                       'id INTEGER PRIMARY KEY,series TEXT NOT NULL UNIQUE,payload BLOB NOT NULL)')
        self.compact = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='flow_templates'").fetchone() is not None
        self.templates = OrderedDict()
        self.ids = {}
        self.cache_bytes = 0
        self.cache_limit = cache_bytes

    def _remember(self, identifier, template):
        key = template[0]['series_key']
        size = _size((identifier, template, key)) + 256
        if size > self.cache_limit:
            return template
        while self.templates and (len(self.templates) >= 128 or self.cache_bytes + size > self.cache_limit):
            _, (old_template, old_size) = self.templates.popitem(last=False)
            self.ids.pop(old_template[0]['series_key'], None)
            self.cache_bytes -= old_size
        self.templates[identifier] = (template, size)
        self.ids[key] = identifier
        self.cache_bytes += size
        return template

    def _template(self, identifier):
        cached = self.templates.get(identifier)
        if cached is not None:
            self.templates.move_to_end(identifier)
            return cached[0]
        saved = self.db.execute('SELECT payload FROM flow_templates WHERE id=?', (identifier,)).fetchone()
        if saved is None or not isinstance(saved[0], bytes) or not saved[0].startswith(_TEMPLATE_MAGIC):
            raise ValueError('unknown or invalid flow template')
        template = _plain(_unbinary(saved[0][len(_TEMPLATE_MAGIC):]))
        if not isinstance(template, list) or len(template) != 2 or not isinstance(template[0], dict) or not isinstance(template[1], list):
            raise ValueError('invalid flow template')
        return self._remember(identifier, template)

    def _encode_template(self, value):
        key = value['template']['series_key']
        identifier = self.ids.get(key)
        if identifier is None:
            saved = self.db.execute('SELECT id FROM flow_templates WHERE series=?', (key,)).fetchone()
            if saved:
                identifier = saved[0]
            else:
                template = _plain([value['template'], sorted(value['metrics'])])
                encoded = _binary(template)
                cursor = self.db.execute('INSERT INTO flow_templates(series,payload) VALUES (?,?)',
                                         (key, _TEMPLATE_MAGIC + encoded))
                identifier = cursor.lastrowid
                self._remember(identifier, _unbinary(encoded))
        return identifier, self._template(identifier)

    def encode(self, value):
        if not self.compact:
            return db_dump(value)
        identifier, (baseline, metric_names) = self._encode_template(value)
        patch = _plain({key: item for key, item in value['template'].items()
                        if key not in baseline or not _same(item, baseline[key])})
        references = []
        default_file = baseline.get('ref',{}).get('source_file')
        for reference in value['refs']:
            ref = reference['ref']
            mask = sum(1 << index for index,name in enumerate(_REF_FIELDS) if name in ref)
            positions = tuple(ref.get(name,0) for name in _REF_POSITIONS)
            packed_positions = _POSITIONS.pack(*positions) if all(type(item) is int and 0 <= item < 2**64 for item in positions) else list(positions)
            cells = ref.get('_cells')
            if isinstance(cells,tuple):cells=list(cells)
            references.append([reference['time'],mask,ref.get('record_id'),
                               None if ref.get('source_file') == default_file else ref.get('source_file'),
                               packed_positions,cells,_extras(ref,_REF_SET),
                               _extras(reference,frozenset(('time','ref')))])
        names = sorted(value['metrics'])
        metrics = []
        for name in names:
            metric = value['metrics'][name]
            metrics.append([*(_integer(metric[field]) for field in _COUNTS),
                            *(metric[field] for field in _NUMBERS),
                            *(list(metric[field]) if metric[field] is not None else None for field in _POINTS),
                            _extras(metric, _METRIC_FIELDS)])
        body = [identifier, patch, sorted(set(baseline) - set(value['template'])),
                _integer(value['count']), value['flags'], references,
                'last_time_seen' in value, value.get('last_time_seen'),
                names if names != metric_names else None, metrics, _extras(value, _VALUE_FIELDS)]
        return MAGIC + zlib.compress(_binary(body), 1)

    def decode(self, payload):
        if not isinstance(payload, bytes) or not payload.startswith(MAGIC):
            return load_json(payload)
        try:
            body = _unbinary(_inflate(payload[len(MAGIC):]))
            if not isinstance(body, list) or len(body) != 11 or not self.compact:
                raise ValueError('invalid flow window body')
            identifier, patch, removed, count, flags, references, has_time, stamp, names, metrics, extras = body
            if type(identifier) is not int or identifier < 1:
                raise ValueError('invalid flow template ID')
            baseline, metric_names = self._template(identifier)
            if not isinstance(patch, dict) or not isinstance(removed, list) or not isinstance(flags, list) or not isinstance(references, list) or type(has_time) is not bool or not isinstance(metrics, list):
                raise ValueError('invalid flow window fields')
            template = _copy(baseline)
            for key in removed:
                if not isinstance(key, str) or key not in template:
                    raise ValueError('invalid removed flow template field')
                template.pop(key)
            _validate(patch)
            if any(type(flag) is not str for flag in flags):raise ValueError('invalid flow flags')
            template.update(patch)
            value = dict(template=template, count=_integer(count), flags=flags, refs=[], metrics={})
            default_file = baseline.get('ref',{}).get('source_file')
            for reference in references:
                if type(reference) is not list or len(reference) != 8:raise ValueError('invalid flow reference')
                time,mask,identifier,file,positions,cells,ref_extra,extra = reference
                if type(time) is not str or type(mask) is not int or not 0 <= mask < 256:raise ValueError('invalid flow reference header')
                if mask & 1 and type(identifier) is not str:raise ValueError('invalid flow reference ID')
                file = default_file if file is None else file
                if mask & 2 and type(file) is not str:raise ValueError('invalid flow reference file')
                if type(positions) is bytes:
                    if len(positions) != _POSITIONS.size:raise ValueError('invalid flow positions')
                    positions = _POSITIONS.unpack(positions)
                elif type(positions) is not list or len(positions) != 5 or any(type(item) is not int or item < 0 for item in positions):raise ValueError('invalid flow positions')
                if mask & 128 and (type(cells) is not list or any(type(cell) is not str for cell in cells)):raise ValueError('invalid flow raw cells')
                ref = dict(record_id=identifier,source_file=file,record_index=positions[0],
                           line_start=positions[1],line_end=positions[2],byte_start=positions[3],
                           byte_length=positions[4],_cells=cells)
                if mask != 255:
                    for index,name in enumerate(_REF_FIELDS):
                        if not mask & (1 << index):ref.pop(name)
                if ref_extra is not None:
                    _check_extras(ref_extra,_REF_SET);ref.update(ref_extra)
                decoded = dict(time=time,ref=ref)
                if extra is not None:
                    _check_extras(extra,frozenset(('time','ref')));decoded.update(extra)
                value['refs'].append(decoded)
            if has_time:
                if type(stamp) is not str:raise ValueError('invalid flow last time')
                value['last_time_seen'] = stamp
            names = metric_names if names is None else names
            if not isinstance(names, list) or any(not isinstance(name, str) for name in names) or len(set(names)) != len(names) or len(metrics) != len(names):
                raise ValueError('invalid flow metric names')
            for name, packed in zip(names, metrics):
                if not isinstance(packed, list) or len(packed) != len(_COUNTS) + len(_NUMBERS) + len(_POINTS) + 1:
                    raise ValueError('invalid flow moments')
                metric = dict(zip((*_COUNTS, *_NUMBERS, *_POINTS), packed[:-1]))
                for field in _COUNTS:
                    _integer(metric[field])
                for field in _NUMBERS:
                    number = metric[field]
                    if number is not None and (type(number) not in (int, float) or not math.isfinite(number)):
                        raise ValueError('invalid flow moment number')
                for field in _POINTS:
                    point = metric[field]
                    if point is not None:
                        if type(point) is not list or len(point) != 4 or type(point[0]) is not str or type(point[1]) is not str or type(point[2]) is not int or point[2] < 0 or type(point[3]) not in (int,float) or not math.isfinite(point[3]):raise ValueError('invalid flow point')
                if packed[-1] is not None:
                    _check_extras(packed[-1],_METRIC_FIELDS);metric.update(packed[-1])
                value['metrics'][name] = metric
            if extras is not None:
                _check_extras(extras,_VALUE_FIELDS);value.update(extras)
            return value
        except (KeyError, IndexError, TypeError, OverflowError) as error:
            raise ValueError('invalid typed flow payload') from error

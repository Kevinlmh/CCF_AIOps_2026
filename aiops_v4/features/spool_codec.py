"""Compact private observations, with legacy prepared-JSON spool compatibility.

The SQL row carries UTC time and record index. A per-series template interns
identity, dimensions, metric names, source path and usual flags. Each payload
retains exact raw metric cells and SHA256 evidence ID, plus typed byte anchors.
Templates are an encoding baseline only: row variants carry explicit patches.
"""
from collections import OrderedDict

from aiops_v4.storage import db_dump, load_json

MAGIC = b'V4O1'
_STATIC = ('batch', 'series_key', 'source', 'view', 'identity', 'dimensions')
_POSITIONS = ('line_start', 'line_end', 'byte_start', 'byte_length')
_REF_FIELDS = frozenset(('record_id', 'source_file', 'record_index', *_POSITIONS))
_ROW_FIELDS = frozenset((*_STATIC, 'timestamp', 'raw_values', 'flags', 'ref'))
_CACHE_SIZE = 2048


def _compact(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observation_templates'").fetchone() is not None


def series_keys(db):
    """Return canonical series strings for either private spool generation."""
    query = ('SELECT series FROM observation_templates ORDER BY series' if _compact(db)
             else 'SELECT DISTINCT series FROM observations ORDER BY series')
    return [row[0] for row in db.execute(query)]


def series_id(db, key):
    """Resolve a canonical series to this file's INTEGER or legacy TEXT key."""
    query = ('SELECT id FROM observation_templates WHERE series=?' if _compact(db)
             else 'SELECT series FROM observations WHERE series=? LIMIT 1')
    result = db.execute(query, (key,)).fetchone()
    return result[0] if result else None


def _pack_uint(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError('invalid observation position')
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return result


def _unpack_uint(payload, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(payload):
            raise ValueError('truncated observation position')
        cell = payload[offset]
        offset += 1
        value |= (cell & 127) << shift
        if not cell & 128:
            if value >= 2**64:
                raise ValueError('invalid observation position')
            return value, offset
    raise ValueError('invalid observation position')


def _remember(cache, key, value):
    cache[key] = value
    if len(cache) > _CACHE_SIZE:
        cache.popitem(last=False)
    return value


class ObservationCodec:
    """Encode rows in a writable file spool; decode both old and new spools.

    ``encode(row)`` returns ``(series_id, payload)``. ``decode(series_id,
    time, idx, payload)`` returns precisely the former prepared record. Create
    the compact template table only for a newly created observations table;
    committed legacy checkpoints continue using their original representation.
    """
    def __init__(self, db, create=False):
        self.db = db
        if create:
            db.execute('CREATE TABLE IF NOT EXISTS observation_templates('
                       'id INTEGER PRIMARY KEY,series TEXT NOT NULL UNIQUE,payload BLOB NOT NULL)')
        self.compact = _compact(db)
        self.templates = OrderedDict()
        self.ids = OrderedDict()

    def _template(self, identifier):
        value = self.templates.get(identifier)
        if value is None:
            saved = self.db.execute('SELECT payload FROM observation_templates WHERE id=?', (identifier,)).fetchone()
            if saved is None:
                raise ValueError('unknown observation template')
            value = _remember(self.templates, identifier, load_json(saved[0]))
        return value

    def encode(self, row):
        if not self.compact:
            return row['series_key'], db_dump(row)
        key = row['series_key']
        identifier = self.ids.get(key)
        if identifier is None:
            saved = self.db.execute('SELECT id FROM observation_templates WHERE series=?', (key,)).fetchone()
            if saved:
                identifier = saved[0]
            else:
                template = {name: row[name] for name in _STATIC}
                template.update(metric_names=list(row['raw_values']), flags=row['flags'],
                                source_file=row['ref']['source_file'])
                cursor = self.db.execute('INSERT INTO observation_templates(series,payload) VALUES (?,?)',
                                         (key, db_dump(template)))
                identifier = cursor.lastrowid
                # Store the serialized copy so callers cannot mutate the cache.
                _remember(self.templates, identifier, load_json(db_dump(template)))
            _remember(self.ids, key, identifier)
        template = self._template(identifier)
        patch = {name: row[name] for name in _STATIC if row[name] != template[name]}
        names = list(row['raw_values'])
        if names != template['metric_names']:
            patch['metric_names'] = names
        if row['ref']['source_file'] != template['source_file']:
            patch['source_file'] = row['ref']['source_file']
        extra_row = {name: value for name, value in row.items() if name not in _ROW_FIELDS}
        if extra_row:
            patch['extra_row'] = extra_row
        flags = row['flags'] if row['flags'] != template['flags'] else None
        extras = {name: value for name, value in row['ref'].items() if name not in _REF_FIELDS}
        ref = row['ref']
        try:
            identifier_bytes = bytes.fromhex(ref['record_id'])
        except (ValueError, TypeError) as error:
            raise ValueError('invalid observation content ID') from error
        if len(identifier_bytes) != 32:
            raise ValueError('invalid observation content ID')
        payload = bytearray(MAGIC + identifier_bytes)
        for name in _POSITIONS:
            payload.extend(_pack_uint(ref[name]))
        payload.extend(db_dump([list(row['raw_values'].values()), flags, patch or None, extras or None]))
        return identifier, bytes(payload)

    def decode(self, series, time, idx, payload):
        if not isinstance(payload, bytes) or not payload.startswith(MAGIC):
            return load_json(payload)
        if len(payload) < len(MAGIC) + 32:
            raise ValueError('truncated observation content ID')
        template = self._template(series)
        offset = len(MAGIC) + 32
        positions = {}
        for name in _POSITIONS:
            positions[name], offset = _unpack_uint(payload, offset)
        body = load_json(payload[offset:])
        if not isinstance(body, list) or len(body) != 4:
            raise ValueError('invalid observation payload')
        values, flags, patch, extras = body
        patch = patch or {}
        names = patch.get('metric_names', template['metric_names'])
        if not isinstance(values, list) or len(values) != len(names):
            raise ValueError('invalid observation metric values')
        row = {}
        for name in _STATIC:
            value = patch.get(name, template[name])
            row[name] = dict(value) if isinstance(value, dict) else value
        row.update(timestamp=time, raw_values=dict(zip(names, values)),
                   flags=list(template['flags'] if flags is None else flags))
        row['ref'] = dict(record_id=payload[len(MAGIC):len(MAGIC)+32].hex(),
                          source_file=patch.get('source_file', template['source_file']),
                          record_index=idx, **positions)
        row['ref'].update(extras or {})
        row.update(patch.get('extra_row', {}))
        return row

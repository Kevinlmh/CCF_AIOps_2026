"""Prepared feature transfer: local dictionaries, typed headers and numeric data.

Workers retain a bounded dictionary cache backed by their private SQLite spool.
Parents remap only local dictionary IDs; full window JSON is never transferred.
"""
from collections import OrderedDict
import json
import struct

from aiops_v4.storage import db_dump, load_json

METRIC_MAGIC = b"V4M3"
REF_MAGIC = b"V4R1"
_METRIC = struct.Struct("<IH")
_REFERENCE = struct.Struct("<I32s5Q")
GRAIN_FIELDS = ("schema_version", "batch", "source", "view", "identity", "dimensions", "reference_limit")
_WINDOW_FIELDS = frozenset((*GRAIN_FIELDS, "window_id", "start_time", "end_time", "record_count", "metrics", "coverage", "quality_flags", "references", "references_truncated"))


def series_key(window, batch):
    return json.dumps([batch, window["source"], window["identity"]["entity_id"], window["dimensions"]], ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_group_grain(payload, batch, series, state_group, entity, source):
    """Validate one opaque local grain before copying it to the final store."""
    grain=load_json(payload)
    if (not isinstance(grain,dict) or any(key not in grain for key in GRAIN_FIELDS) or
            grain['schema_version']!=1 or grain['batch']!=batch or grain['source']!=source or
            not isinstance(grain['identity'],dict) or not isinstance(grain['dimensions'],dict) or
            grain['identity'].get('entity_id')!=entity or not isinstance(entity,str) or not entity or
            not isinstance(grain['view'],str) or not grain['view'] or
            type(grain['reference_limit']) is not int or not 1<=grain['reference_limit']<=8 or
            not isinstance(state_group,str) or not state_group):
        raise ValueError('invalid prepared feature group grain')
    if series_key(grain,batch)!=series:
        raise ValueError('prepared feature group identity mismatch')
    return grain


def window_extras(window):
    result = {key: value for key, value in window.items() if key not in _WINDOW_FIELDS}
    result["coverage"] = {key: value for key, value in window["coverage"].items() if key != "observed_timestamps"}
    return result


def pack_metrics(metrics):
    """Pack (dictionary ID, statistics bytes) preserving metric order."""
    metrics = list(metrics)
    if len(metrics) > 2048:
        raise ValueError("window metric limit exceeded")
    payload = bytearray(METRIC_MAGIC + struct.pack("<H", len(metrics)))
    for identifier, numeric in metrics:
        if not 0 < identifier < 2**32 or not 0 < len(numeric) < 2**16:
            raise ValueError("invalid prepared metric")
        payload.extend(_METRIC.pack(identifier, len(numeric)))
        payload.extend(numeric)
    return bytes(payload)


def iter_metrics(payload):
    """Read new variable statistics frames and historical fixed statistics."""
    from .store import LAYOUT
    if not isinstance(payload, bytes):
        raise ValueError("invalid metrics payload")
    modern = payload.startswith(METRIC_MAGIC)
    offset = len(METRIC_MAGIC) if modern else 0
    if len(payload) < offset + 2:
        raise ValueError("truncated metrics payload")
    count = struct.unpack_from("<H", payload, offset)[0]
    offset += 2
    if count > 2048:
        raise ValueError("invalid metric count")
    for _ in range(count):
        header = _METRIC.size if modern else 4
        if len(payload) < offset + header:
            raise ValueError("truncated metric header")
        if modern:
            identifier, size = _METRIC.unpack_from(payload, offset)
        else:
            identifier, size = struct.unpack_from("<I", payload, offset)[0], LAYOUT.size
        offset += header
        if not identifier or not size or size > LAYOUT.size or len(payload) < offset + size:
            raise ValueError("truncated metric statistics")
        yield identifier, payload[offset:offset+size]
        offset += size
    if offset != len(payload):
        raise ValueError("unexpected metrics bytes")


def pack_references(references):
    references = list(references)
    if len(references) > 8:
        raise ValueError("reference limit exceeded")
    return REF_MAGIC + struct.pack("<H", len(references)) + b"".join(_REFERENCE.pack(*ref) for ref in references)


def unpack_references(payload):
    if not isinstance(payload, bytes) or not payload.startswith(REF_MAGIC) or len(payload) < 6:
        raise ValueError("invalid prepared references")
    count = struct.unpack_from("<H", payload, 4)[0]
    if count > 8 or len(payload) != 6 + count*_REFERENCE.size:
        raise ValueError("invalid prepared references size")
    return [_REFERENCE.unpack_from(payload, 6 + index*_REFERENCE.size) for index in range(count)]


def canonicalize_window(window, manifests, batch):
    """Recover content SHA IDs only for finite retained raw references."""
    from aiops_v4.data.position import record_id
    anchors = {}; refs = []
    for ref in window["references"]:
        if ref["record_id"].startswith("position:"):
            manifest = manifests[ref["source_file"]]
            cells = ref["_cells"]
            raw = {name: cells[index] if index < len(cells) else None for index, name in enumerate(manifest["columns"])}
            ref = dict(ref, record_id=record_id(batch, manifest["path"], ref["record_index"], raw, tuple(cells[len(manifest["columns"]):])))
        ref = {key: value for key, value in ref.items() if key != "_cells"}
        anchors[ref["record_id"]] = ref
        refs.append({key: ref[key] for key in ("record_id", "source_file", "record_index", "line_start", "line_end")})
    window["references"] = refs
    return window, anchors


class PreparedWindowWriter:
    def __init__(self, db, batch, *, cache_size=2048):
        if type(cache_size) is not int or cache_size < 1:
            raise ValueError("positive prepared dictionary cache size required")
        self.db, self.batch, self.cache_size = db, batch, cache_size
        self.caches = {table: OrderedDict() for table in ("groups", "definitions", "flags", "extras", "files")}
        db.executescript("""BEGIN; PRAGMA user_version=4;
          CREATE TABLE groups(id INTEGER PRIMARY KEY,key TEXT UNIQUE,state_group TEXT,entity TEXT,source TEXT,payload BLOB NOT NULL);
          CREATE TABLE definitions(id INTEGER PRIMARY KEY,key TEXT UNIQUE,payload BLOB NOT NULL);
          CREATE TABLE flags(id INTEGER PRIMARY KEY,key TEXT UNIQUE,payload BLOB NOT NULL);
          CREATE TABLE extras(id INTEGER PRIMARY KEY,key TEXT UNIQUE,payload BLOB NOT NULL);
          CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT UNIQUE);
          CREATE TABLE feature_blocks(id INTEGER PRIMARY KEY,grp INTEGER,windows INTEGER NOT NULL,raw_size INTEGER NOT NULL,payload BLOB NOT NULL);
          CREATE TABLE windows(id BLOB,grp INTEGER,start INTEGER,end INTEGER,records INTEGER,observed INTEGER,
            truncated INTEGER,flags INTEGER,statistics BLOB,extras INTEGER,refs BLOB,position INTEGER PRIMARY KEY);
        """)
        self._pending=[];self._pending_entries=0;self._pending_group=None;self._block_id=1

    def _remember(self, table, key, identifier):
        cache = self.caches[table]
        cache[key] = identifier
        if len(cache) > self.cache_size:
            cache.popitem(last=False)
        return identifier

    def _intern(self, table, value):
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        cache = self.caches[table]
        if key in cache:
            return cache[key]
        saved = self.db.execute(f"SELECT id FROM {table} WHERE key=?", (key,)).fetchone()
        identifier = saved[0] if saved else self.db.execute(f"INSERT INTO {table}(key,payload) VALUES (?,?)", (key, db_dump(value))).lastrowid
        return self._remember(table, key, identifier)

    def _file(self, path):
        cache = self.caches["files"]
        if path in cache:
            return cache[path]
        saved = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
        identifier = saved[0] if saved else self.db.execute("INSERT INTO files(path) VALUES (?)", (path,)).lastrowid
        return self._remember("files", path, identifier)

    def _group(self, window):
        key = series_key(window, self.batch)
        cache = self.caches["groups"]
        if key in cache:
            return cache[key]
        saved = self.db.execute("SELECT id FROM groups WHERE key=?", (key,)).fetchone()
        if saved:
            identifier = saved[0]
        else:
            from aiops_v4.states.matrix import window_vector
            grain = {name: window[name] for name in GRAIN_FIELDS}
            identifier = self.db.execute("INSERT INTO groups(key,state_group,entity,source,payload) VALUES (?,?,?,?,?)", (key, window_vector(window, self.batch)["group_id"], window["identity"]["entity_id"], window["source"], db_dump(grain))).lastrowid
        return self._remember("groups", key, identifier)

    def add(self, window, anchors):
        from .store import micros
        from .block_codec import fixed_statistics,dense_size,MAX_WINDOWS,MAX_BLOCK_BYTES,TARGET_BLOCK_ENTRIES,pack_token
        group = self._group(window)
        metrics = []
        for name, metric in window["metrics"].items():
            numeric, extra = fixed_statistics(metric, finite=True)
            identifier = self._intern("definitions", dict(name=name, extra=extra))
            metrics.append((identifier, numeric))
        references = []
        for ref in window["references"]:
            anchor = anchors[ref["record_id"]]
            digest = bytes.fromhex(ref["record_id"])
            if len(digest) != 32:
                raise ValueError("invalid feature reference digest")
            references.append((self._file(ref["source_file"]), digest, *(anchor[key] for key in ("record_index", "line_start", "line_end", "byte_start", "byte_length"))))
        if self._pending and (group!=self._pending_group or len(self._pending)==MAX_WINDOWS or self._pending_entries+len(metrics)>TARGET_BLOCK_ENTRIES or dense_size(len(self._pending)+1,self._pending_entries+len(metrics))>MAX_BLOCK_BYTES):
            self.flush()
        if len(metrics)>2048 or dense_size(1,len(metrics))>MAX_BLOCK_BYTES:
            raise ValueError("feature window exceeds block size limit")
        identifier=bytes.fromhex(window["window_id"])
        if len(identifier)!=32:raise ValueError("invalid feature content digest")
        self.db.execute("INSERT INTO windows(id,grp,start,end,records,observed,truncated,flags,statistics,extras,refs) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (identifier, group, micros(window["start_time"]), micros(window["end_time"]), window["record_count"], window["coverage"]["observed_timestamps"], window["references_truncated"], self._intern("flags", window["quality_flags"]), pack_token(self._block_id,len(self._pending)), self._intern("extras", window_extras(window)), pack_references(references)))
        self._pending.append(metrics);self._pending_entries+=len(metrics);self._pending_group=group
        if len(self._pending)==MAX_WINDOWS:self.flush()

    def flush(self):
        """Persist the partial block before caller commits its private spool."""
        if not self._pending:return
        from .block_codec import encode_block,pack_storage
        raw=encode_block(self._pending)
        self.db.execute("INSERT INTO feature_blocks VALUES (?,?,?,?,?)", (self._block_id,self._pending_group,len(self._pending),len(raw),pack_storage(raw,compress=False)))
        self._block_id+=1;self._pending=[];self._pending_entries=0;self._pending_group=None

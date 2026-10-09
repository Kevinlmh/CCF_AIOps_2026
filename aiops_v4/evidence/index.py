"""Batch-scoped read-only tools over full native observations and memberships."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from aiops_v4.data.reader import utc_time
from aiops_v4.states.pipeline import _dump, _load
from .raw import resolve_references, validate_reference


def create_index(path):
    db = sqlite3.connect(path)
    db.executescript('''
        PRAGMA foreign_keys=ON;
        PRAGMA temp_store=FILE;
        PRAGMA cache_size=-8192;
        PRAGMA user_version=2;
        CREATE TABLE metadata(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE models(grp TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE states(id TEXT PRIMARY KEY,entity TEXT,grp TEXT REFERENCES models(grp),
          source TEXT,start TEXT,end TEXT,candidate INTEGER,status TEXT,quality INTEGER,payload TEXT NOT NULL);
        CREATE TABLE matrices(id TEXT PRIMARY KEY REFERENCES states(id),payload TEXT NOT NULL);
        CREATE TABLE events(id TEXT PRIMARY KEY,entity TEXT,grp TEXT REFERENCES models(grp),
          start TEXT,end TEXT,payload TEXT NOT NULL);
        CREATE TABLE event_windows(id TEXT PRIMARY KEY REFERENCES states(id),event TEXT REFERENCES events(id));
        CREATE TABLE bundles(id TEXT PRIMARY KEY,entity TEXT,start TEXT,end TEXT,payload TEXT NOT NULL);
        CREATE TABLE members(event TEXT PRIMARY KEY REFERENCES events(id),bundle TEXT REFERENCES bundles(id));
        CREATE TABLE relations(bundle TEXT REFERENCES bundles(id),other TEXT REFERENCES bundles(id),relation TEXT,
          payload TEXT NOT NULL,PRIMARY KEY(bundle,other,relation));
        CREATE TABLE anchors(id TEXT PRIMARY KEY,file TEXT,idx INTEGER,payload TEXT NOT NULL,UNIQUE(file,idx));
        CREATE TABLE wanted_raw(id TEXT PRIMARY KEY REFERENCES anchors(id));
        CREATE TABLE raw_records(id TEXT PRIMARY KEY REFERENCES anchors(id),payload TEXT NOT NULL);
        CREATE INDEX state_entity_time ON states(entity,start,end);
        CREATE INDEX state_group_time ON states(grp,start,end);
        CREATE INDEX event_entity_time ON events(entity,start,end);
        CREATE INDEX bundle_entity_time ON bundles(entity,start,end);
        CREATE INDEX bundle_members ON members(bundle,event);
        CREATE INDEX event_window_members ON event_windows(event,id);
    ''')
    return db


def put_metadata(db, key, value):
    db.execute('INSERT INTO metadata VALUES (?,?)', (key, _dump(value)))


def metadata(db, key):
    row = db.execute('SELECT payload FROM metadata WHERE key=?', (key,)).fetchone()
    if row is None:
        raise ValueError('missing evidence metadata: ' + key)
    return _load(row[0])


def scoring_provenance(summary):
    return {key: summary[key] for key in ('reference_scope', 'config', 'input_scope',
                                         'input_summary_sha256', 'input_windows_sha256')}


@contextmanager
def read_index(database, batch):
    if not isinstance(batch, str) or not batch.strip():
        raise ValueError('nonempty batch required')
    path = Path(database).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA user_version').fetchone()[0] != 2 or metadata(db, 'batch') != batch:
            raise ValueError('evidence database schema/batch mismatch; rebuild old packet contracts into a new directory')
        yield db
    finally:
        db.close()


def query_evidence(database, batch, kind, *, bundle_id=None, entity_id=None, source=None,
                   group_id=None, start=None, end=None, limit=100, offset=0):
    if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or not 0 <= offset <= 2**63-1:
        raise ValueError('limit must be 1..1000 and offset in SQLite integer range')
    start = utc_time(start, require_timezone=True)[0] if start is not None else None
    end = utc_time(end, require_timezone=True)[0] if end is not None else None
    if start is not None and end is not None and start >= end:
        raise ValueError('query interval must be increasing')
    definitions = {
        'bundle': ('SELECT t.payload FROM bundles t', {'bundle_id': 't.id', 'entity_id': 't.entity'}, 't.entity,t.start,t.id'),
        'members': ('SELECT t.payload FROM events t JOIN members m ON m.event=t.id',
                    {'bundle_id': 'm.bundle', 'entity_id': 't.entity', 'group_id': 't.grp'}, 't.start,t.end,t.id'),
        'states': ('SELECT t.payload,x.payload FROM states t JOIN matrices x ON x.id=t.id',
                   {'entity_id': 't.entity', 'source': 't.source', 'group_id': 't.grp'}, 't.start,t.source,t.grp,t.id'),
        'model': ('SELECT t.payload FROM models t', {'group_id': 't.grp'}, 't.grp'),
        'relations': ('SELECT t.payload FROM relations t', {'bundle_id': 't.bundle'}, 't.other,t.relation')}
    if kind not in definitions:
        raise ValueError('unknown evidence query kind')
    if kind in {'members', 'relations'} and bundle_id is None:
        raise ValueError('bundle_id required for membership/relation queries')
    query, supported, ordering = definitions[kind]
    predicates, values = [], []
    for key, value in dict(bundle_id=bundle_id, entity_id=entity_id, source=source, group_id=group_id).items():
        if value is None:
            continue
        if key not in supported or not isinstance(value, str) or not value:
            raise ValueError('unsupported/invalid filter for query kind: ' + key)
        predicates.append(supported[key] + '=?')
        values.append(value)
    if (start is not None or end is not None) and kind not in {'bundle', 'members', 'states'}:
        raise ValueError('time filter unavailable for this query kind')
    if start is not None:
        predicates.append('t.end>?'); values.append(start)
    if end is not None:
        predicates.append('t.start<?'); values.append(end)
    if predicates:
        query += ' WHERE ' + ' AND '.join(predicates)
    query += ' ORDER BY ' + ordering + ' LIMIT ? OFFSET ?'
    with read_index(database, batch) as db:
        provenance = scoring_provenance(metadata(db, 'state_summary'))
        if bundle_id is not None and db.execute('SELECT 1 FROM bundles WHERE id=?', (bundle_id,)).fetchone() is None:
            raise ValueError('unknown bundle_id')
        rows = []
        for row in db.execute(query, (*values, limit, offset)):
            result = _load(row[0])
            if kind == 'states':
                result['matrix'] = _load(row[1])
            result['scoring_provenance'] = provenance
            rows.append(result)
        return rows


def query_raw(database, batch, record_id=None, *, reference=None):
    """Return a frozen verified row or reverify an unmaterialized registered anchor."""
    if (record_id is None) == (reference is None):
        raise ValueError('supply either record_id or reference')
    with read_index(database, batch) as db:
        files = metadata(db, 'raw_manifest')
        if reference is not None:
            ref = validate_reference(reference, {f['path']: f for f in files})
            if reference.get('batch', batch) != batch:
                raise ValueError('raw reference batch mismatch')
            record_id = ref['record_id']
        row = db.execute('SELECT payload FROM anchors WHERE id=?', (record_id,)).fetchone()
        if row is None:
            raise ValueError('unknown/unregistered raw reference')
        registered = _load(row[0])
        if reference is not None and ref != registered:
            raise ValueError('raw reference differs from registered provenance')
        saved = db.execute('SELECT payload FROM raw_records WHERE id=?', (record_id,)).fetchone()
        if saved is not None:
            result = _load(saved[0])
            result['retrieval'] = 'verified_materialized_snapshot'
            return result
        rows = list(resolve_references(metadata(db, 'raw_root'), batch, [registered], files, metadata(db, 'naive_timezone')))
        result = rows[0]
        result['retrieval'] = 'verified_from_registered_raw_file'
        return result

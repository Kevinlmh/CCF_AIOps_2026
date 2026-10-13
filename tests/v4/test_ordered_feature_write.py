"""Physical feature format and typed worker transfer preserve public semantics."""
import copy
import sqlite3
import struct

import pytest

from aiops_v4.features import store
from aiops_v4.storage import db_dump
from test_store_writer_batch import FILES, sample, prepared


def bits(value):
    return struct.pack("<d", value)


def test_new_windows_have_integer_physical_key_and_narrow_compatible_references(tmp_path):
    _, db, writer = prepared(tmp_path)
    writer.add_many([sample(3), sample(1), sample(2)])
    assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    columns = db.execute("PRAGMA table_info(windows)").fetchall()
    assert columns[-1][1:3] == ("seq", "INTEGER") and columns[-1][5] == 1
    assert db.execute("SELECT seq,hex(id) FROM windows ORDER BY seq").fetchall() == [
        (index, f"{number:064x}".upper()) for index, number in enumerate((3, 1, 2), 1)]
    assert db.execute("SELECT typeof(window) FROM _window_refs").fetchall() == [("integer",)]*3
    assert db.execute("SELECT typeof(window) FROM window_refs").fetchall() == [("blob",)]*3
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_lossless_compact_statistics_preserve_bits_absence_null_and_int64():
    metric = {key: 1 for key in store.INTS}
    metric.update({key: 3.125 for key in store.FLOATS})
    metric.update({key: True for key in store.BOOLS})
    metric.update(mean=-0.0, std=0.0, median=None, delta_sum=23.25,
                  count=2**63-1, unknown={"keep": [None, False, "x"]})
    del metric["counter_gap_count"]
    blob, extra = store.pack_statistics(metric)
    assert len(blob) < store.LAYOUT.size//2
    decoded = store.unpack_statistics(blob, extra)
    assert decoded.keys() == metric.keys()
    for key, value in metric.items():
        assert bits(decoded[key]) == bits(value) if isinstance(value, float) else decoded[key] == value
    projection = store.unpack_statistics(blob, extra, statistics_fields=("mean", "std", "median", "delta_sum", "counter_gap_count"))
    assert set(projection) == {"mean", "std", "median", "delta_sum", "unknown"}
    assert bits(projection["mean"]) == bits(-0.0)
    assert projection["median"] is None and projection["delta_sum"] == 23.25


@pytest.mark.parametrize("value", [0.0, -0.0, 1.5, 1e300])
def test_single_sample_and_constant_statistics_keep_exact_float_bits(value):
    metric = {key: value for key in store.FLOATS}
    metric.update({key: 0 for key in store.INTS})
    blob, extra = store.pack_statistics(metric)
    assert len(blob) < store.LAYOUT.size//2
    assert all(bits(item) == bits(value) for key, item in store.unpack_statistics(blob, extra).items() if key in store.FLOATS)


def test_legacy_fixed_statistics_decode_and_projection_remain_supported():
    value = {"count": 1, "mean": -0.0, "median": None, "semantics": {"kind": "gauge"}}
    present = (1 << store.FIELDS.index("count")) | (1 << store.FIELDS.index("mean")) | (1 << store.FIELDS.index("median"))
    nonnull = present & ~(1 << store.FIELDS.index("median"))
    values = [value.get(key) or 0 for key in store.FIELDS]
    values[store.FIELDS.index("mean")] = -0.0
    blob = store.LAYOUT.pack(1, present, nonnull, *values)
    decoded = store.unpack_statistics(blob, {"semantics": value["semantics"]})
    assert bits(decoded["mean"]) == bits(-0.0)
    assert decoded == value
    assert store.unpack_statistics(blob, {}, statistics_fields=("count",)) == {"count": 1}


def test_compact_statistics_reject_truncated_and_invalid_payloads():
    blob, extra = store.pack_statistics({"count": 1, "mean": 2.0})
    for damaged in (blob[:-1], blob+b"x", b"\x7f"+blob[1:]):
        with pytest.raises((ValueError, struct.error)):
            store.unpack_statistics(damaged, extra)
    with pytest.raises(ValueError):
        store.pack_statistics({"count": 2**63})


def test_prepared_worker_dictionary_remap_roundtrip_without_json_window(tmp_path, monkeypatch):
    from aiops_v4.features.write_codec import PreparedWindowWriter
    path, db, writer = prepared(tmp_path)
    worker = sqlite3.connect(tmp_path/"worker.sqlite")
    prepared_writer = PreparedWindowWriter(worker, "stage1")
    pairs = [sample(10), sample(20, entity="other", anchor_number=11)]
    pairs[1][0]["metrics"]["cpu_usage"]["mean"] = -0.0
    for window, anchors in pairs:
        prepared_writer.add(window, anchors)
    prepared_writer.flush(); worker.commit()
    assert worker.execute("PRAGMA user_version").fetchone()[0] == 4
    assert worker.execute("SELECT typeof(start),typeof(statistics) FROM windows").fetchall() == [("integer", "blob")]*2
    original_load = store.load_json
    def dictionary_only(payload):
        value = original_load(payload)
        assert not isinstance(value, dict) or "window_id" not in value
        return value
    monkeypatch.setattr(store, "load_json", dictionary_only)
    writer.import_prepared(worker, batch_size=1)
    db.commit(); db.close(); worker.close()
    with store.FeatureStore(path, verify=False) as reader:
        actual = reader.get_windows([row[0]["window_id"] for row in pairs])
        assert actual == {window["window_id"]: window for window, _ in pairs}
        assert bits(actual[pairs[1][0]["window_id"]]["metrics"]["cpu_usage"]["mean"]) == bits(-0.0)


def test_prepared_worker_failure_rolls_back_all_chunks_and_cache_ids(tmp_path):
    from aiops_v4.features.write_codec import PreparedWindowWriter
    _, db, writer = prepared(tmp_path)
    worker = sqlite3.connect(tmp_path/"worker.sqlite")
    output = PreparedWindowWriter(worker, "stage1")
    window, anchors = sample(1, entity="rolledback")
    output.add(window, anchors); output.add(copy.deepcopy(window), anchors)
    output.flush(); worker.commit()
    with pytest.raises(sqlite3.IntegrityError):
        writer.import_prepared(worker, batch_size=1)
    assert db.execute("SELECT count(*) FROM windows").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM groups").fetchone()[0] == 0
    writer.add(*sample(3))
    assert db.execute("SELECT seq FROM windows").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    worker.close(); db.close()


def test_incompatible_checkpoint_resume_rejected_but_legacy_sealed_format_readable(tmp_path):
    path, db, writer = prepared(tmp_path)
    writer.add(*sample(1)); db.commit()
    db.execute("PRAGMA user_version=2")
    with pytest.raises(ValueError, match="version-four"):
        store.StoreWriter.resume(db, "stage1")
    db.close()
    with store.FeatureStore(path, verify=False) as reader:
        assert reader.get_windows([f"{1:064x}"])[f"{1:064x}"] == sample(1)[0]


@pytest.mark.parametrize('version', [1, 2])
def test_real_legacy_ten_column_windows_fixed_statistics_and_wide_refs(tmp_path, version):
    import zlib
    path=tmp_path/f'legacy_{version}.sqlite'
    db=sqlite3.connect(path)
    db.executescript('''
      CREATE TABLE metadata(key TEXT PRIMARY KEY,payload BLOB NOT NULL);
      CREATE TABLE groups(id INTEGER PRIMARY KEY,series TEXT,state_group TEXT,entity TEXT,source TEXT,payload BLOB);
      CREATE TABLE definitions(id INTEGER PRIMARY KEY,payload BLOB);
      CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,payload BLOB);
      CREATE TABLE flags(id INTEGER PRIMARY KEY,payload BLOB);
      CREATE TABLE extras(id INTEGER PRIMARY KEY,payload BLOB);
      CREATE TABLE windows(id BLOB PRIMARY KEY,grp INTEGER,start INTEGER,end INTEGER,records INTEGER,observed INTEGER,truncated INTEGER,flags INTEGER,statistics BLOB,extras BLOB) WITHOUT ROWID;
      CREATE TABLE anchors(id INTEGER PRIMARY KEY,digest BLOB,file INTEGER,payload BLOB);
      CREATE TABLE window_refs(window BLOB,position INTEGER,anchor INTEGER,PRIMARY KEY(window,position)) WITHOUT ROWID;
    ''')
    db.execute(f'PRAGMA user_version={version}')
    window, anchors=sample(1)
    from aiops_v4.features.write_codec import GRAIN_FIELDS, window_extras
    grain={key:window[key] for key in GRAIN_FIELDS}
    db.execute('INSERT INTO groups VALUES (1,?,?,?,?,?)', ('series', 'state-group', 'br1', 'node', db_dump(grain)))
    db.execute('INSERT INTO files VALUES (1,?,?)', (FILES[0]['path'], db_dump(FILES[0])))
    db.execute("INSERT INTO metadata VALUES ('summary',?)", (db_dump(dict(batch='stage1',input_root=str(tmp_path))),))
    numeric,extra=store.pack_statistics(window['metrics']['cpu_usage'])
    metric=window['metrics']['cpu_usage']
    present=sum(1<<index for index,key in enumerate(store.FIELDS) if key in metric)
    nonnull=sum(1<<index for index,key in enumerate(store.FIELDS) if key in metric and metric[key] is not None)
    fixed=store.LAYOUT.pack(1,present,nonnull,*[0 if metric.get(key) is None else metric[key] for key in store.FIELDS])
    db.execute('INSERT INTO definitions VALUES (1,?)',(db_dump(dict(name='cpu_usage',extra=extra)),))
    db.execute('INSERT INTO flags VALUES (1,?)',(db_dump([]),))
    extras=db_dump(window_extras(window))
    db.execute('INSERT INTO extras VALUES (1,?)',(extras,))
    identifier=bytes.fromhex(window['window_id'])
    db.execute('INSERT INTO windows VALUES (?,?,?,?,?,?,?,?,?,?)',(identifier,1,store.micros(window['start_time']),store.micros(window['end_time']),1,1,0,1,zlib.compress(struct.pack('<HI',1,1)+fixed,1),extras if version==1 else 1))
    anchor=next(iter(anchors.values()))
    payload=db_dump(anchor) if version==1 else store.ANCHOR.pack(*(anchor[key] for key in ('record_index','line_start','line_end','byte_start','byte_length')))
    db.execute('INSERT INTO anchors VALUES (1,?,?,?)',(bytes.fromhex(anchor['record_id']),1,payload))
    db.execute('INSERT INTO window_refs VALUES (?,0,1)',(identifier,))
    db.commit();db.close()
    with store.FeatureStore(path,verify=False) as reader:
        assert list(reader.windows()) == [window]
        assert reader.get_windows([window['window_id']]) == {window['window_id']:window}
        assert reader.anchor(anchor['record_id'])[0] == anchor
        projected=list(reader.windows(statistics_fields=('mean',),include_references=False))[0]
        assert projected['metrics']['cpu_usage'] == dict(extra,mean=0.0)


def test_worker_dictionary_caches_remain_bounded_with_many_variants(tmp_path):
    from aiops_v4.features.write_codec import PreparedWindowWriter
    db=sqlite3.connect(tmp_path/'many.sqlite')
    writer=PreparedWindowWriter(db,'stage1',cache_size=2)
    for number in range(1,7):
        window,anchors=sample(number)
        window['metrics']['cpu_usage']['unknown']={'variant':number%3}
        window['custom']={'variant':number%3}
        writer.add(window,anchors)
    assert all(len(cache)<=2 for cache in writer.caches.values())
    assert db.execute('SELECT count(*) FROM definitions').fetchone()[0]==3
    assert db.execute('SELECT count(*) FROM extras').fetchone()[0]==3
    db.close()


@pytest.mark.parametrize('damage', ['version', 'present_mask', 'nonnull_mask', 'truncated', 'varint', 'palette', 'boolean', 'nonfinite', 'fixed_boolean', 'fixed_nonfinite'])
def test_prepared_import_rejects_corrupt_numeric_semantics_before_persisting(tmp_path, damage):
    from aiops_v4.features.write_codec import PreparedWindowWriter, iter_metrics, pack_metrics
    _, db, writer=prepared(tmp_path)
    source=sqlite3.connect(':memory:');worker=PreparedWindowWriter(source,'stage1')
    worker.add(*sample(1));worker.add(*sample(2))
    metric={'count':1,'mean':1.0,'quantiles_exact':True}
    numeric,extra=store.pack_statistics(metric)
    if damage.startswith('fixed_'):
        present=(1<<store.FIELDS.index('count'))|(1<<store.FIELDS.index('mean'))|(1<<store.FIELDS.index('quantiles_exact'))
        values=[0]*len(store.FIELDS);values[0]=1;values[store.FIELDS.index('mean')]=1.0;values[store.FIELDS.index('quantiles_exact')]=True
        numeric=store.LAYOUT.pack(1,present,present,*values)
    damaged=bytearray(numeric)
    if damage=='version':damaged[0]=127
    elif damage=='present_mask':struct.pack_into('<Q',damaged,1,1<<63)
    elif damage=='nonnull_mask':struct.pack_into('<Q',damaged,9,(1<<63))
    elif damage=='truncated':del damaged[-1]
    elif damage=='varint':damaged=damaged[:17]+b'\x80'*10+damaged[18:]
    elif damage=='palette':damaged[-2]=255
    elif damage=='boolean':damaged[-1]=4
    elif damage=='nonfinite':damaged=bytearray(store.pack_statistics(dict(metric,mean=float('inf')))[0])
    elif damage=='fixed_boolean':damaged[-2]=2
    elif damage=='fixed_nonfinite':struct.pack_into('<d',damaged,17+8*(len(store.INTS)+store.FLOATS.index('mean')),float('nan'))
    # Historical prepared frames remain an import boundary; block tokens are
    # resolved through a database rather than passed to this point codec.
    worker.flush()
    identifier=source.execute('SELECT id FROM definitions ORDER BY id LIMIT 1').fetchone()[0]
    valid,_=store.pack_statistics(sample(1)[0]['metrics']['cpu_usage'])
    source.execute('UPDATE windows SET statistics=? WHERE position=1',(pack_metrics([(identifier,valid)]),))
    source.execute('UPDATE windows SET statistics=? WHERE position=2',(pack_metrics([(identifier,bytes(damaged))]),))
    source.execute('PRAGMA user_version=3');source.commit()
    with pytest.raises(ValueError):writer.import_prepared(source,batch_size=1)
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM groups').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM definitions').fetchone()[0]==0
    writer.add(*sample(3))
    assert db.execute('SELECT seq FROM windows').fetchone()[0]==1
    source.close();db.close()


@pytest.mark.parametrize('payload', [b'V4Z1invalid', db_dump([]), db_dump({'batch':'stage1'}), db_dump({'schema_version':1,'batch':'wrong','source':'node','view':'node','identity':{'entity_id':'br1'},'dimensions':{},'reference_limit':8})])
def test_prepared_import_validates_opaque_group_grain_once(tmp_path, payload):
    from aiops_v4.features.write_codec import PreparedWindowWriter
    _, db, writer=prepared(tmp_path)
    source=sqlite3.connect(':memory:');worker=PreparedWindowWriter(source,'stage1');worker.add(*sample(1))
    worker.flush();source.execute('UPDATE groups SET payload=?',(payload,));source.commit()
    with pytest.raises(ValueError):writer.import_prepared(source)
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM groups').fetchone()[0]==0
    source.close();db.close()

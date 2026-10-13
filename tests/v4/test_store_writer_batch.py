"""Regression coverage for bounded canonical-store writes and crash recovery."""
import copy
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from aiops_v4.features import store
from aiops_v4.storage import db_dump


FILES = [dict(path='node_metrics_a.csv', columns=['timestamp', 'node', 'cpu_usage'])]


def sample(number=1, *, anchor_number=10, entity='br1'):
    anchor = dict(record_id=f'{anchor_number:064x}', source_file=FILES[0]['path'],
                  record_index=anchor_number, line_start=anchor_number+1,
                  line_end=anchor_number+1, byte_start=100+anchor_number, byte_length=17)
    ref = {key: anchor[key] for key in ('record_id', 'source_file', 'record_index', 'line_start', 'line_end')}
    window = dict(schema_version=1, batch='stage1', source='node', view='node',
                  identity=dict(entity_id=entity, role='router'), dimensions={}, reference_limit=8,
                  window_id=f'{number:064x}', start_time='2026-09-17T04:00:00.000000Z',
                  end_time='2026-09-17T04:01:00.000000Z', record_count=1,
                  metrics={'cpu_usage': dict(count=1, mean=0.0, median=None, quantiles_exact=True,
                      semantics=dict(kind='gauge', unit='percent', status='verified'), unknown={'unit': 'x'})},
                  coverage=dict(observed_timestamps=1, fraction=1.0), quality_flags=[],
                  references=[ref], references_truncated=False, custom={'unchanged': True})
    return window, {anchor['record_id']: anchor}


def prepared(tmp_path, **kwargs):
    path = tmp_path/'features.sqlite'
    db = store.create_store(path)
    writer = store.StoreWriter(db, 'stage1', FILES, **kwargs)
    db.execute("INSERT INTO metadata VALUES ('summary',?)", (db_dump({'batch': 'stage1', 'input_root': str(tmp_path)}),))
    db.commit()
    return path, db, writer


def test_add_many_roundtrips_ids_statistics_unknown_fields_and_shared_anchor(tmp_path):
    assert callable(getattr(store.StoreWriter, 'add_many', None)), 'bounded canonical batch writer is missing'
    path, db, writer = prepared(tmp_path)
    pairs = [sample(1), sample(2), sample(3, anchor_number=11, entity='br2')]
    writer.add_many(iter(pairs), batch_size=2)
    db.commit()
    assert db.execute('SELECT count(*) FROM anchors').fetchone()[0] == 2
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    db.close()
    with store.FeatureStore(path, verify=False) as reader:
        assert reader.get_windows([w['window_id'] for w, _ in pairs]) == {w['window_id']: w for w, _ in pairs}
        anchor, manifest = reader.anchor(f'{10:064x}')
        assert anchor == pairs[0][1][f'{10:064x}']
        assert manifest == FILES[0]


def test_add_many_flushes_before_consuming_the_next_bounded_batch(tmp_path):
    assert callable(getattr(store.StoreWriter, 'add_many', None)), 'bounded canonical batch writer is missing'
    _, db, writer = prepared(tmp_path)
    def rows():
        for number in range(1, 8):
            if number in (3, 5, 7):
                assert db.execute('SELECT count(*) FROM windows').fetchone()[0] == number-1
            yield sample(number)
    writer.add_many(rows(), batch_size=2)
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0] == 7
    db.close()


def test_shared_anchor_id_cache_avoids_repeated_selects_and_stays_bounded(tmp_path):
    assert callable(getattr(store.StoreWriter, 'add_many', None)), 'bounded canonical batch writer is missing'
    _, db, writer = prepared(tmp_path, anchor_cache_size=2)
    statements = []
    db.set_trace_callback(statements.append)
    writer.add_many((sample(n) for n in range(1, 8)), batch_size=2)
    lookups = [s for s in statements if s.upper().startswith('SELECT') and 'FROM ANCHORS' in s.upper()]
    assert len(lookups) <= 1
    writer.add_many((sample(n, anchor_number=n+20) for n in range(8, 13)), batch_size=2)
    assert len(writer.anchor_ids) <= 2
    assert db.execute('SELECT count(*) FROM window_refs').fetchone()[0] == 12
    db.close()


def test_batch_failure_rolls_back_every_chunk_and_restores_intern_caches(tmp_path):
    assert callable(getattr(store.StoreWriter, 'add_many', None)), 'bounded canonical batch writer is missing'
    _, db, writer = prepared(tmp_path)
    first, anchors = sample(1, entity='rolled-back')
    first['quality_flags'] = ['temporary_flag']
    first['custom'] = {'temporary': True}
    with pytest.raises(sqlite3.IntegrityError):
        writer.add_many([(first, anchors), (copy.deepcopy(first), anchors)], batch_size=1)
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM groups').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM anchors').fetchone()[0] == 0
    writer.add(*sample(2))
    db.commit()
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0] == 1
    db.close()


def test_reopened_writer_preserves_existing_anchor_and_dictionary_ids(tmp_path):
    assert callable(getattr(store.StoreWriter, 'resume', None)), 'canonical writer resume is missing'
    path, db, writer = prepared(tmp_path)
    writer.add(*sample(1))
    db.commit()
    before = db.execute('SELECT id,digest,payload FROM anchors').fetchall()
    db.close()
    db = sqlite3.connect(path)
    resumed = store.StoreWriter.resume(db, 'stage1', anchor_cache_size=2)
    resumed.add_many([sample(2)], batch_size=1)
    db.commit()
    assert db.execute('SELECT id,digest,payload FROM anchors').fetchall() == before
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT count(*) FROM groups').fetchone()[0] == 1
    db.close()


def test_write_connection_applies_budget_and_restores_reopened_pragmas(tmp_path):
    assert callable(getattr(store, 'configure_write_connection', None)), 'consistent write connection policy is missing'
    path = tmp_path/'policy.sqlite'
    db = sqlite3.connect(path)
    store.configure_write_connection(db, memory_bytes=32*1024**3, workers=8)
    assert db.execute('PRAGMA cache_size').fetchone()[0] == -512*1024
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert db.execute('PRAGMA synchronous').fetchone()[0] == 2
    assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    assert db.execute('PRAGMA temp_store').fetchone()[0] == 1
    db.close()
    db = sqlite3.connect(path)
    store.configure_write_connection(db, cache_bytes=24*1024**2)
    assert db.execute('PRAGMA cache_size').fetchone()[0] == -24*1024
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    with pytest.raises(ValueError):
        store.configure_write_connection(db, memory_bytes=-1, workers=8)
    db.close()


def test_crash_before_group_commit_leaves_no_windows_or_progress(tmp_path):
    assert callable(getattr(store.StoreWriter, 'add_many', None)), 'bounded canonical batch writer is missing'
    path, db, writer = prepared(tmp_path)
    writer.add(*sample(1))
    db.execute("INSERT INTO progress VALUES ('committed')")
    db.commit()
    db.close()
    script = '''
import os, sqlite3, sys
from aiops_v4.features.store import StoreWriter, configure_write_connection
from test_store_writer_batch import sample
conn=sqlite3.connect(sys.argv[1])
configure_write_connection(conn, cache_bytes=64*1024)
writer=StoreWriter.resume(conn,'stage1')
writer.add_many((sample(n,anchor_number=n+10) for n in range(2,2002)),batch_size=128)
conn.execute("INSERT INTO progress VALUES ('unfinished')")
assert os.path.getsize(sys.argv[1]+'-journal')>512
os._exit(23)
'''
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join([str(Path.cwd()), str(Path(__file__).parent)])
    result = subprocess.run([sys.executable, '-c', script, str(path)], env=env, capture_output=True, text=True)
    assert result.returncode == 23, result.stderr
    db = sqlite3.connect(path)
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0] == 1
    assert db.execute('SELECT grp FROM progress').fetchall() == [('committed',)]
    assert db.execute('SELECT count(*) FROM anchors').fetchone()[0] == 1
    db.close()


def test_version_one_json_anchor_and_extra_payload_remain_readable(tmp_path):
    path, db, _ = prepared(tmp_path)
    window, anchors = sample(1)
    writer = store.StoreWriter.resume(db, 'stage1') if hasattr(store.StoreWriter, 'resume') else store.StoreWriter.__new__(store.StoreWriter)
    if not hasattr(store.StoreWriter, 'resume'):
        writer.db, writer.batch = db, 'stage1'
        writer.groups, writer.definitions, writer.flags, writer.extras = {}, {}, {}, {}
        writer.file_ids = {FILES[0]['path']: 1}
    writer.add(window, anchors)
    extra = db.execute('SELECT payload FROM extras WHERE id=1').fetchone()[0]
    db.execute('UPDATE windows SET extras=?', (extra,))
    db.execute('UPDATE anchors SET payload=?', (db_dump(anchors[f'{10:064x}']),))
    db.execute('PRAGMA user_version=1')
    db.commit()
    db.close()
    with store.FeatureStore(path, verify=False) as reader:
        assert list(reader.windows()) == [window]
        assert reader.anchor(f'{10:064x}')[0] == anchors[f'{10:064x}']


def test_policy_refuses_to_silently_ignore_foreign_keys_inside_transaction(tmp_path):
    assert callable(getattr(store, 'configure_write_connection', None)), 'consistent write connection policy is missing'
    db = sqlite3.connect(tmp_path/'active.sqlite')
    db.execute('CREATE TABLE t(value INTEGER)')
    db.execute('INSERT INTO t VALUES (1)')
    with pytest.raises(ValueError, match='transaction'):
        store.configure_write_connection(db, cache_bytes=8*1024**2)
    assert db.in_transaction
    db.close()


def test_write_policy_keeps_an_existing_wal_journal(tmp_path):
    assert callable(getattr(store, 'configure_write_connection', None)), 'consistent write connection policy is missing'
    db = sqlite3.connect(tmp_path/'wal.sqlite')
    assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    store.configure_write_connection(db, cache_bytes=8*1024**2)
    assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert db.execute('PRAGMA synchronous').fetchone()[0] == 2
    db.close()


def test_autocommit_connection_still_leaves_group_boundary_for_the_caller(tmp_path):
    assert callable(getattr(store.StoreWriter, 'add_many', None)), 'bounded canonical batch writer is missing'
    path, db, _ = prepared(tmp_path)
    db.close()
    db = sqlite3.connect(path, isolation_level=None)
    writer = store.StoreWriter.resume(db, 'stage1')
    writer.add_many([sample(1)], batch_size=1)
    assert db.in_transaction
    other = sqlite3.connect(path)
    assert other.execute('SELECT count(*) FROM windows').fetchone()[0] == 0
    other.close()
    db.rollback()
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0] == 0
    db.close()


def test_duplicate_digest_in_one_batch_preserves_the_first_anchor_payload(tmp_path):
    _, db, writer = prepared(tmp_path)
    first = sample(1)
    second = sample(2)
    second[1][f'{10:064x}']['byte_start'] = 9999
    writer.add_many([first, second], batch_size=2)
    payload = db.execute('SELECT payload FROM anchors').fetchone()[0]
    assert store.ANCHOR.unpack(payload)[3] == 110
    db.close()


def test_batch_error_preserves_writes_before_its_savepoint(tmp_path):
    _, db, writer = prepared(tmp_path)
    writer.add(*sample(1))
    with pytest.raises(sqlite3.IntegrityError):
        writer.add_many([sample(2), sample(2)], batch_size=1)
    assert db.execute('SELECT hex(id) FROM windows').fetchall() == [(f'{1:064x}'.upper(),)]
    writer.add(*sample(3))
    db.commit()
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0] == 2
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    db.close()


def test_sqlite_automatic_transaction_rollback_preserves_the_original_error(tmp_path):
    _, db, writer = prepared(tmp_path)
    writer.add(*sample(1))
    db.execute("INSERT INTO progress VALUES ('committed')")
    db.commit()
    def rows():
        yield sample(2, entity='rolled-back')
        # SQLITE_FULL/INTERRUPT can also roll back a transaction automatically;
        # OR ROLLBACK provides a deterministic real-SQL reproduction here.
        db.execute("INSERT OR ROLLBACK INTO progress VALUES ('committed')")
    with pytest.raises(sqlite3.IntegrityError):
        writer.add_many(rows(), batch_size=1)
    assert not db.in_transaction
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0] == 1
    writer.add(*sample(3, entity='new-after-rollback'))
    db.commit()
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0] == 2
    db.close()


@pytest.mark.parametrize('mode',['OFF','MEMORY'])
def test_write_policy_refuses_a_disabled_rollback_journal(tmp_path,mode):
    db = sqlite3.connect(tmp_path/'unsafe.sqlite')
    assert db.execute('PRAGMA journal_mode='+mode).fetchone()[0] == mode.lower()
    with pytest.raises(ValueError, match='journal'):
        store.configure_write_connection(db, cache_bytes=8*1024**2)
    assert db.execute('PRAGMA journal_mode').fetchone()[0] == mode.lower()
    db.close()


def legacy_payloads(db, pairs):
    db.execute('UPDATE windows SET extras=(SELECT payload FROM extras WHERE id=windows.extras)')
    for _, anchors in pairs:
        for identifier, anchor in anchors.items():
            db.execute('UPDATE anchors SET payload=? WHERE digest=?', (db_dump(anchor), bytes.fromhex(identifier)))
    db.execute('PRAGMA user_version=1')


@pytest.mark.parametrize('version',[1,2])
def test_statistics_only_window_pass_skips_reference_tables_and_preserves_other_fields(tmp_path,version):
    import inspect
    assert 'include_references' in inspect.signature(store.FeatureStore.windows).parameters, 'statistics-only window pass is missing'
    path, db, writer = prepared(tmp_path)
    pairs = [sample(1), sample(2, anchor_number=11)]
    writer.add_many(pairs)
    if version == 1:legacy_payloads(db,pairs)
    db.commit();db.close()
    with store.FeatureStore(path,verify=False) as reader:
        complete = list(reader.windows(page_size=1))
        statements = []
        reader.db.set_trace_callback(statements.append)
        partial = list(reader.windows(page_size=1,include_references=False))
        assert not any('WINDOW_REFS' in s.upper() or 'ANCHORS' in s.upper() for s in statements)
        assert partial == [dict(window,references=[]) for window in complete]
        assert all(window['references'] for window in complete)
        # The explicit group path must obey the same skip request.
        group = reader.db.execute('SELECT state_group FROM groups').fetchone()[0]
        assert list(reader.windows(group,include_references=False)) == partial
        assert list(reader.windows()) == complete


@pytest.mark.parametrize('version',[1,2])
def test_decode_rows_can_skip_reference_join_without_changing_window_statistics(tmp_path,version):
    import inspect
    assert 'include_references' in inspect.signature(store.FeatureStore.decode_rows).parameters, 'statistics-only decoder is missing'
    path, db, writer = prepared(tmp_path)
    pair = sample(1)
    writer.add(*pair)
    if version == 1:legacy_payloads(db,[pair])
    db.commit();db.close()
    with store.FeatureStore(path,verify=False) as reader:
        rows = reader.db.execute('SELECT * FROM windows').fetchall()
        statements = []
        reader.db.set_trace_callback(statements.append)
        decoded = reader.decode_rows(rows,include_references=False)
        assert decoded == [dict(pair[0],references=[])]
        assert not any('WINDOW_REFS' in s.upper() or 'ANCHORS' in s.upper() for s in statements)
        assert reader.decode_rows(rows) == [pair[0]]


def test_build_connection_selects_durable_wal_and_a_bounded_default_checkpoint(tmp_path):
    assert callable(getattr(store,'configure_build_connection',None)), 'durable WAL build policy is missing'
    db=sqlite3.connect(tmp_path/'wal_build.sqlite')
    db.execute('PRAGMA wal_autocheckpoint=17')
    budget=store.configure_build_connection(db,memory_bytes=32*1024**3,workers=8)
    assert budget==512*1024**2
    assert db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    assert db.execute('PRAGMA synchronous').fetchone()[0]==2
    assert db.execute('PRAGMA foreign_keys').fetchone()[0]==1
    assert db.execute('PRAGMA cache_size').fetchone()[0]==-524288
    assert db.execute('PRAGMA wal_autocheckpoint').fetchone()[0]==1000
    db.execute('CREATE TABLE t(value INTEGER)');db.execute('INSERT INTO t VALUES (1)')
    with pytest.raises(ValueError,match='transaction'):
        store.configure_build_connection(db,cache_bytes=8*1024**2)
    assert db.in_transaction
    db.close()


def test_build_publication_checkpoint_leaves_a_complete_single_database(tmp_path):
    assert callable(getattr(store,'finalize_build_connection',None)), 'WAL build publication boundary is missing'
    path,db,writer=prepared(tmp_path)
    store.configure_build_connection(db,cache_bytes=64*1024)
    writer.add_many((sample(n,anchor_number=n+10) for n in range(1,2001)),batch_size=128)
    db.execute("INSERT INTO progress VALUES ('complete')")
    with pytest.raises(ValueError,match='transaction'):
        store.finalize_build_connection(db)
    assert db.in_transaction
    db.commit()
    store.finalize_build_connection(db)
    assert db.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
    db.close()
    assert not any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal'))
    published=tmp_path/'published.sqlite';path.rename(published)
    reader=sqlite3.connect(published.as_uri()+'?mode=ro',uri=True)
    assert reader.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
    assert reader.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert reader.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert reader.execute('SELECT count(*) FROM windows').fetchone()[0]==2000
    assert reader.execute('SELECT count(*) FROM anchors').fetchone()[0]==2000
    assert reader.execute('SELECT grp FROM progress').fetchall()==[('complete',)]
    reader.close()


def test_active_reader_blocks_publication_and_preserves_recoverable_wal(tmp_path):
    assert callable(getattr(store,'finalize_build_connection',None)), 'WAL build publication boundary is missing'
    path,db,writer=prepared(tmp_path)
    store.configure_build_connection(db,cache_bytes=64*1024)
    db.execute('PRAGMA busy_timeout=1')
    writer.add(*sample(1));db.commit()
    reader=sqlite3.connect(path)
    reader.execute('BEGIN')
    assert reader.execute('SELECT count(*) FROM windows').fetchone()[0]==1
    writer.add(*sample(2,anchor_number=11));db.commit()
    with pytest.raises(RuntimeError,match='checkpoint'):
        store.finalize_build_connection(db)
    assert db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    assert Path(str(path)+'-wal').exists()
    assert Path(str(path)+'-shm').exists()
    assert reader.execute('SELECT count(*) FROM windows').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0]==2
    reader.rollback();reader.close()
    store.finalize_build_connection(db);db.close()
    assert not any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal'))
    reader=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    assert reader.execute('SELECT count(*) FROM windows').fetchone()[0]==2
    reader.close()


def test_low_cache_wal_crash_recovers_only_atomic_committed_group_progress(tmp_path):
    assert callable(getattr(store,'configure_build_connection',None)), 'durable WAL build policy is missing'
    path,db,_=prepared(tmp_path);db.close()
    script='''
import os, sqlite3, sys
from pathlib import Path
from aiops_v4.features.store import StoreWriter, configure_build_connection
from test_store_writer_batch import sample
conn=sqlite3.connect(sys.argv[1])
configure_build_connection(conn,cache_bytes=64*1024)
writer=StoreWriter.resume(conn,'stage1')
writer.add(*sample(1))
conn.execute("INSERT INTO progress VALUES ('committed')")
conn.commit()
writer.add_many((sample(n,anchor_number=n+10) for n in range(2,2002)),batch_size=128)
conn.execute("INSERT INTO progress VALUES ('unfinished')")
assert Path(sys.argv[1]+'-wal').stat().st_size>64*1024
os._exit(29)
'''
    env=dict(os.environ)
    env['PYTHONPATH']=os.pathsep.join([str(Path.cwd()),str(Path(__file__).parent)])
    child=subprocess.run([sys.executable,'-c',script,str(path)],env=env,capture_output=True,text=True)
    assert child.returncode==29,child.stderr
    assert Path(str(path)+'-wal').exists()
    db=sqlite3.connect(path)
    store.configure_build_connection(db,cache_bytes=64*1024)
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert db.execute('SELECT count(*) FROM windows').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM anchors').fetchone()[0]==1
    assert db.execute('SELECT grp FROM progress').fetchall()==[('committed',)]
    resumed=store.StoreWriter.resume(db,'stage1')
    resumed.add(*sample(2,anchor_number=11))
    db.execute("INSERT INTO progress VALUES ('resumed')");db.commit()
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    store.finalize_build_connection(db);db.close()
    reader=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    assert reader.execute('SELECT count(*) FROM windows').fetchone()[0]==2
    assert reader.execute('SELECT grp FROM progress ORDER BY grp').fetchall()==[('committed',),('resumed',)]
    reader.close()


STATE_STATISTICS_FIELDS=('last','min','max','mean','delta_sum','rate_mean','change_count',
                         'counter_decrease_count','counter_gap_count','sum',
                         'invalid_input_count','conflicting_time_count')


def vector_metrics():
    values={key:3 for key in store.INTS}
    values.update({key:1.25 for key in store.FLOATS})
    values.update({key:True for key in store.BOOLS})
    values.update(mean=1.5,last=2.0,sum=4.0,rate_mean=0.5,delta_sum=3.0,
                  invalid_input_count=0,conflicting_time_count=0,
                  counter_decrease_count=0,counter_gap_count=0,
                  unknown={'unit':'preserved','nested':[0,None,False]})
    def metric(kind='gauge',unit='percent',**changes):
        return dict(values,semantics=dict(kind=kind,unit=unit,status='verified'),**changes)
    return dict(gauge=metric(),category=metric(unit='state_code'),counter=metric(kind='counter',unit='bytes'),
                interval=metric(kind='interval_count',unit='packets'),
                broken_counter=metric(kind='counter',unit='bytes',counter_decrease_count=1),
                invalid_gauge=metric(invalid_input_count=2),conflicting_gauge=metric(conflicting_time_count=1),
                null_gauge=metric(mean=None),nonfinite_counter=metric(kind='counter',unit='bytes',rate_mean=float('inf')),
                missing_gauge={'count':1,'semantics':dict(kind='gauge',unit='percent',status='verified'),
                               'unknown':{'unit':'missing-preserved'}},
                excluded=metric(kind='unknown',unit='opaque'))


def test_statistics_projection_preserves_zero_null_boolean_absence_and_unknown_metadata():
    import inspect
    assert 'statistics_fields' in inspect.signature(store.unpack_statistics).parameters, 'typed statistics projection is missing'
    original=dict(count=0,mean=0.0,median=None,quantiles_exact=False,p95=9.0,unknown={'unit':'custom'})
    blob,metadata=store.pack_statistics(original)
    before=copy.deepcopy(metadata)
    selected=store.unpack_statistics(blob,metadata,statistics_fields=('count','mean','median','min','quantiles_exact'))
    assert selected==dict(count=0,mean=0.0,median=None,quantiles_exact=False,unknown={'unit':'custom'})
    assert metadata==before
    assert store.unpack_statistics(blob,metadata)==original
    assert store.unpack_statistics(blob,metadata,statistics_fields=())=={'unknown':{'unit':'custom'}}


@pytest.mark.parametrize('bad_fields',[('mean_typo',),('semantics',),'mean',('mean',None)])
def test_statistics_projection_rejects_unknown_or_invalid_field_names(bad_fields):
    import inspect
    assert 'statistics_fields' in inspect.signature(store.unpack_statistics).parameters, 'typed statistics projection is missing'
    blob,metadata=store.pack_statistics(dict(count=1,mean=2.0,semantics={'kind':'gauge'}))
    with pytest.raises(ValueError,match='statistics'):
        store.unpack_statistics(blob,metadata,statistics_fields=bad_fields)


@pytest.mark.parametrize('version',[1,2])
def test_projected_window_vectors_equal_complete_vectors_for_every_state_metric_case(tmp_path,version):
    import inspect
    from aiops_v4.states.matrix import window_vector
    assert 'statistics_fields' in inspect.signature(store.FeatureStore.windows).parameters, 'window statistics projection is missing'
    path,db,writer=prepared(tmp_path)
    pair=sample(1);pair[0]['metrics']=vector_metrics()
    writer.add(*pair)
    if version==1:legacy_payloads(db,[pair])
    db.commit();db.close()
    with store.FeatureStore(path,verify=False) as reader:
        complete=list(reader.windows())
        before=copy.deepcopy(reader.definitions)
        projected=list(reader.windows(statistics_fields=STATE_STATISTICS_FIELDS))
        assert reader.definitions==before
        assert len(projected[0]['metrics']['gauge'])==len(STATE_STATISTICS_FIELDS)+2
        assert 'count' not in projected[0]['metrics']['gauge']
        assert 'p95' not in projected[0]['metrics']['gauge']
        assert projected[0]['metrics']['gauge']['unknown']=={'unit':'preserved','nested':[0,None,False]}
        assert 'mean' not in projected[0]['metrics']['missing_gauge']
        assert projected[0]['metrics']['null_gauge']['mean'] is None
        expected=window_vector(complete[0],'stage1')
        actual=window_vector(projected[0],'stage1')
        assert actual==expected
        assert actual['features']['gauge::mean']['value']==1.5
        assert actual['features']['category::last']['value']=='2'
        assert actual['features']['nonfinite_counter::rate_mean']['value'] is None
        assert actual['features']['broken_counter::rate_mean']['value'] is None
        assert actual['features']['invalid_gauge::mean']['value'] is None
        assert actual['features']['missing_gauge::mean']['value'] is None
        assert actual['excluded_metrics']==['excluded']
        first_pass=list(reader.windows(include_references=False,statistics_fields=STATE_STATISTICS_FIELDS))
        assert window_vector(first_pass[0],'stage1')==dict(expected,references=[])
        rows=reader.db.execute('SELECT * FROM windows').fetchall()
        assert reader.decode_rows(rows,statistics_fields=list(STATE_STATISTICS_FIELDS))==projected
        assert reader._decode(rows[0],complete[0]['references'],statistics_fields=STATE_STATISTICS_FIELDS)==projected[0]
        assert list(reader.windows())==complete
        assert reader.get_windows([pair[0]['window_id']])[pair[0]['window_id']]==complete[0]
        assert reader.anchor(f'{10:064x}')[0]==pair[1][f'{10:064x}']
        assert set(complete[0]['metrics']['gauge'])==set(store.FIELDS)|{'semantics','unknown'}
        assert store.pack_statistics(complete[0]['metrics']['gauge'])==store.pack_statistics(pair[0]['metrics']['gauge'])


def test_window_statistics_projection_rejects_unknown_names_even_for_an_empty_page(tmp_path):
    import inspect
    assert 'statistics_fields' in inspect.signature(store.FeatureStore.decode_rows).parameters, 'row statistics projection is missing'
    path,db,_=prepared(tmp_path);db.close()
    with store.FeatureStore(path,verify=False) as reader:
        with pytest.raises(ValueError,match='statistics'):
            reader.decode_rows([],statistics_fields=('no_such_statistic',))
        with pytest.raises(ValueError,match='statistics'):
            list(reader.windows(statistics_fields=('no_such_statistic',)))

from contextlib import closing
from pathlib import Path
import json
import pytest
from test_feature_store import fixture, compare
from aiops_v4.features.pipeline import build_features
from aiops_v4.features.build import build_feature_store
from aiops_v4.features.store import FeatureStore


def test_full_feature_build_never_copies_all_sources_into_merged_spool(tmp_path):
    raw = tmp_path / 'raw'
    fixture(raw)
    out = tmp_path / 'new'
    observations = []
    def inspect(progress):
        if progress.get('event') == 'group_complete':
            observations.append(progress)
            assert not (out / '.work' / 'merged.sqlite').exists(), 'duplicate merged observations spool still exists'
    result = build_feature_store(raw, 'stage1', out, naive_timezone='UTC', workers=1, min_free_bytes=0, progress=inspect)
    assert observations
    assert result['rows_scanned'] == 37
    assert result['intermediate_policy'] == 'private_spools_direct_merge'


def test_direct_stream_preserves_cross_file_values_ids_and_resume(tmp_path):
    raw = tmp_path / 'raw'
    fixture(raw)
    old = tmp_path / 'old'
    out = tmp_path / 'new'
    build_features(raw, 'stage1', old, naive_timezone='UTC', netflow_streaming=True)
    def stop(progress):
        if progress.get('event') == 'group_complete':
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        build_feature_store(raw, 'stage1', out, naive_timezone='UTC', workers=1, min_free_bytes=0, progress=stop)
    events = []
    build_feature_store(raw, 'stage1', out, naive_timezone='UTC', workers=1, min_free_bytes=0, resume=True, progress=events.append)
    assert any(e.get('event') == 'group_reused' for e in events)
    expected = [json.loads(line) for line in (old / 'windows.jsonl').read_text().splitlines()]
    with FeatureStore(out / 'features.sqlite') as store:
        compare(expected, list(store.windows()))
        for window in store.windows():
            for ref in window['references']:
                assert store.raw_record(ref['record_id'])['record_id'] == ref['record_id']


def test_flow_source_connections_close_when_writer_fails_with_retained_traceback(tmp_path, monkeypatch):
    import os
    from test_records import write_csv
    from aiops_v4.features.store import StoreWriter
    raw = tmp_path / 'raw'
    write_csv(raw, 'netflow.csv', [dict(minute_utc='2026-09-17T04:00:00Z', node='br1', bytes='5') for _ in range(2)])
    out = tmp_path / 'new'
    def fail(self, rows, **kwargs):
        next(iter(rows))
        raise RuntimeError('test writer interruption')
    monkeypatch.setattr(StoreWriter, 'add_many', fail)
    failures = []
    try:
        build_feature_store(raw, 'stage1', out, naive_timezone='UTC', workers=1, min_free_bytes=0)
    except RuntimeError as error:
        failures.append(error)
    assert failures
    if not Path('/proc/self/fd').exists(): pytest.skip('Linux FD verification')
    leaked = []
    for fd in Path('/proc/self/fd').iterdir():
        try: name = os.readlink(fd)
        except OSError: continue
        if name.startswith(str(out / '.work')): leaked.append(name)
    assert not leaked, leaked


def test_feature_build_uses_durable_wal_and_publishes_standalone_database(tmp_path):
    import sqlite3
    raw=tmp_path/'raw';fixture(raw);out=tmp_path/'new';seen=[]
    def inspect(progress):
        if progress.get('event')=='group_complete':
            with closing(sqlite3.connect((out/'.features.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
                mode=db.execute('PRAGMA journal_mode').fetchone()[0]
            seen.append(mode)
            assert mode=='wal', 'rotational bulk-build still uses rollback journal'
    build_feature_store(raw,'stage1',out,naive_timezone='UTC',workers=1,min_free_bytes=0,progress=inspect)
    assert seen
    with closing(sqlite3.connect((out/'features.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
        assert db.execute('SELECT count(*) FROM windows').fetchone()[0]>0
    assert not any(p.name.endswith(('-wal','-shm')) for p in out.iterdir())

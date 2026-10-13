from contextlib import closing
import sqlite3
from test_records import write_csv
from aiops_v4.features.build import build_feature_store
from aiops_v4.states.build import build_state_store
from aiops_v4.features.store import FeatureStore


def test_state_writer_uses_budgeted_cache_and_resumed_connection_policy(tmp_path, monkeypatch):
    raw = tmp_path / 'raw'
    write_csv(raw, 'node_metrics.csv', [dict(timestamp=f'2026-09-17T04:{i:02d}:00Z', node='br1', cpu_usage=str(i)) for i in range(16)])
    feature = tmp_path / 'features'
    build_feature_store(raw, 'stage1', feature, naive_timezone='UTC', workers=1, min_free_bytes=0)
    actual = sqlite3.connect
    targets = []
    def connect(path, *args, **kwargs):
        db = actual(path, *args, **kwargs)
        if str(path).endswith('.states.sqlite'): targets.append(db)
        return db
    monkeypatch.setattr(sqlite3, 'connect', connect)
    def inspect(progress):
        if progress.get('event') == 'group_complete':
            assert targets[-1].execute('PRAGMA cache_size').fetchone()[0] <= -65536
            assert targets[-1].execute('PRAGMA foreign_keys').fetchone()[0] == 1
    build_state_store(feature, 'stage1', tmp_path / 'states', workers=1, min_free_bytes=0, progress=inspect)


def test_reference_and_assessment_share_one_projected_scan_with_references(tmp_path, monkeypatch):
    raw = tmp_path / 'raw'
    write_csv(raw, 'node_metrics.csv', [dict(timestamp=f'2026-09-17T04:{i:02d}:00Z', node='br1', cpu_usage=str(i)) for i in range(16)])
    feature = tmp_path / 'features'
    build_feature_store(raw, 'stage1', feature, naive_timezone='UTC', workers=1, min_free_bytes=0)
    original = FeatureStore.windows
    calls = []
    def windows(self, *args, **kwargs):
        calls.append(kwargs.get('include_references', True))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(FeatureStore, 'windows', windows)
    build_state_store(feature, 'stage1', tmp_path / 'states', workers=1, min_free_bytes=0)
    assert calls == [True]


def test_state_build_uses_wal_then_seals_without_sidecar_dependencies(tmp_path):
    raw=tmp_path/'raw';write_csv(raw,'node_metrics.csv',[dict(timestamp=f'2026-09-17T04:{i:02d}:00Z',node='br1',cpu_usage=str(i)) for i in range(16)])
    feature=tmp_path/'features';out=tmp_path/'states'
    build_feature_store(raw,'stage1',feature,naive_timezone='UTC',workers=1,min_free_bytes=0)
    seen=[]
    def inspect(progress):
        if progress.get('event')=='group_complete':
            with closing(sqlite3.connect((out/'.states.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
                mode=db.execute('PRAGMA journal_mode').fetchone()[0]
            seen.append(mode);assert mode=='wal'
    build_state_store(feature,'stage1',out,workers=1,min_free_bytes=0,progress=inspect)
    assert seen
    with closing(sqlite3.connect((out/'states.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
        assert db.execute('SELECT count(*) FROM deltas').fetchone()[0]==16
    assert not any(p.name.endswith(('-wal','-shm')) for p in out.iterdir())


def test_state_passes_decode_only_metrics_used_by_vector_contract(tmp_path,monkeypatch):
    raw=tmp_path/'raw';write_csv(raw,'node_metrics.csv',[dict(timestamp=f'2026-09-17T04:{i:02d}:00Z',node='br1',cpu_usage=str(i)) for i in range(16)])
    feature=tmp_path/'features';build_feature_store(raw,'stage1',feature,naive_timezone='UTC',workers=1,min_free_bytes=0)
    original=FeatureStore.windows;fields=[]
    def inspect(self,*args,**kwargs):
        fields.append(kwargs.get('statistics_fields'))
        return original(self,*args,**kwargs)
    monkeypatch.setattr(FeatureStore,'windows',inspect)
    build_state_store(feature,'stage1',tmp_path/'states',workers=1,min_free_bytes=0)
    expected={'last','min','max','mean','delta_sum','rate_mean','change_count','counter_decrease_count','counter_gap_count','sum','invalid_input_count','conflicting_time_count'}
    assert len(fields)==1 and all(value is not None and set(value)==expected for value in fields)

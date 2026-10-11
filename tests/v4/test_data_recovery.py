import json
import os
from pathlib import Path
import threading
import pytest

from test_records import write_csv
from test_feature_store import api,compare
from aiops_v4.features.pipeline import build_features
from aiops_v4.storage import load_json


def build_args():return dict(naive_timezone='UTC',workers=1,min_free_bytes=0)


def test_cancellation_boundary_preserves_yielded_row_on_resume(tmp_path,monkeypatch):
    from aiops_v4.data import workers
    raw=tmp_path/'raw'
    write_csv(raw,'node_metrics.csv',[dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage=str(i)) for i in range(1001)])
    build,store=api();out=tmp_path/'new';stop=threading.Event();stop.set();monkeypatch.setattr(workers,'_STOP',stop)
    with pytest.raises(workers.WorkerCancelled):build.build_feature_store(raw,'stage1',out,**build_args())
    stop.clear()
    s=build.build_feature_store(raw,'stage1',out,resume=True,**build_args())
    assert s['rows_scanned']==s['rows_accepted']+s['rows_rejected']==1001
    with store.FeatureStore(out/'features.sqlite') as f:assert sum(w['record_count'] for w in f.windows())==1001


@pytest.mark.parametrize('streaming',[False,True])
def test_netflow_numeric_and_auxiliary_identity_agree_with_legacy(tmp_path,streaming):
    raw=tmp_path/'raw'
    write_csv(raw,'netflow.csv',[dict(minute_utc='2026-09-17T04:00:00Z',hostname='aux-router',source_region='xian',source_ip='1.2.3.4',interface_id='eth0',protocol='6',bytes='5')])
    build,store=api();old=tmp_path/'old';new=tmp_path/'new'
    build_features(raw,'stage1',old,naive_timezone='UTC',netflow_streaming=streaming)
    build.build_feature_store(raw,'stage1',new,netflow_streaming=streaming,**build_args())
    expected=[json.loads(l) for l in (old/'windows.jsonl').read_text().splitlines()]
    with store.FeatureStore(new/'features.sqlite') as f:compare(expected,list(f.windows()))


def test_netflow_interrupted_before_first_checkpoint_resumes(tmp_path,monkeypatch):
    from aiops_v4.features.spool import PreparedParser
    raw=tmp_path/'raw'
    write_csv(raw,'netflow.csv',[dict(minute_utc='2026-09-17T04:00:00Z',node='br1',bytes='5') for _ in range(3)])
    original=PreparedParser.parse
    def interrupt(self,position):
        if position.record_index==2:raise KeyboardInterrupt()
        return original(self,position)
    build,store=api();out=tmp_path/'new';monkeypatch.setattr(PreparedParser,'parse',interrupt)
    with pytest.raises(KeyboardInterrupt):build.build_feature_store(raw,'stage1',out,**build_args())
    monkeypatch.setattr(PreparedParser,'parse',original)
    s=build.build_feature_store(raw,'stage1',out,resume=True,**build_args())
    assert s['rows_accepted']==3
    with store.FeatureStore(out/'features.sqlite') as f:assert sum(w['record_count'] for w in f.windows())==3


def test_netflow_valid_primary_time_cache_ignores_unused_secondary_timestamp(tmp_path,monkeypatch):
    from aiops_v4.data.reader import RecordReader
    raw=tmp_path/'raw'
    write_csv(raw,'netflow.csv',[dict(minute_utc='2026-09-17T04:00:00Z',first_seen=f'2026-09-17T03:{i:02d}:00Z',node='br1',bytes='5') for i in range(30)])
    actual=RecordReader._normalize;calls=[]
    def spy(self,cells,*args):
        if set(cells)<={'minute_utc','first_seen'}:calls.append(cells)
        return actual(self,cells,*args)
    monkeypatch.setattr(RecordReader,'_normalize',spy)
    build,_=api();build.build_feature_store(raw,'stage1',tmp_path/'new',**build_args())
    assert len(calls)==1


def test_netflow_invalid_primary_still_uses_distinct_fallback_times(tmp_path):
    raw=tmp_path/'raw'
    write_csv(raw,'netflow.csv',[dict(minute_utc='bad',first_seen=f'2026-09-17T04:{i:02d}:00Z',node='br1',bytes='5') for i in range(2)])
    build,store=api();old=tmp_path/'old';new=tmp_path/'new'
    build_features(raw,'stage1',old,naive_timezone='UTC',netflow_streaming=True)
    build.build_feature_store(raw,'stage1',new,**build_args())
    expected=[json.loads(l) for l in (old/'windows.jsonl').read_text().splitlines()]
    with store.FeatureStore(new/'features.sqlite') as f:compare(expected,list(f.windows()))


def test_prepared_feature_publication_recovers_without_rebuilding(tmp_path,monkeypatch):
    from aiops_v4.data import cache
    from aiops_v4.features import build
    raw=tmp_path/'raw';write_csv(raw,'node_metrics.csv',[dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage='5')])
    out=tmp_path/'new';original=cache.atomic_json
    def interrupt(path,value):
        if Path(path).name=='seal.json':raise KeyboardInterrupt()
        return original(path,value)
    monkeypatch.setattr(cache,'atomic_json',interrupt)
    with pytest.raises(KeyboardInterrupt):build.build_feature_store(raw,'stage1',out,**build_args())
    before=cache.sha256(out/'features.sqlite')
    assert (out/'summary.json').exists() and not (out/'seal.json').exists()
    monkeypatch.setattr(cache,'atomic_json',original)
    monkeypatch.setattr(build,'parse_file',lambda *_:pytest.fail('prepared layer reparsed'))
    s=build.build_feature_store(raw,'stage1',out,resume=True,**build_args())
    assert s['window_count']==1 and cache.sha256(out/'features.sqlite')==before
    cache.verify_layer(out,verify_raw=True)


@pytest.mark.parametrize('mutation',['code','raw'])
def test_completed_canonical_run_rejects_changed_code_or_raw_bytes(tmp_path,monkeypatch,mutation):
    from test_experiment_config import config
    from aiops_v4.experiments import canonical
    from aiops_v4.experiments.run import run_experiment
    raw=tmp_path/'raw';p=write_csv(raw,'node_metrics.csv',[dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage='5')])
    c=config(raw);c.update(agents={'discovery_only':True},storage={'backend':'canonical','workers':1,'min_free_gib':0})
    out=tmp_path/'run';run_experiment(c,out)
    if mutation=='code':
        old=canonical.provenance
        monkeypatch.setattr(canonical,'provenance',lambda c:dict(old(c),code_sha256='0'*64))
    else:
        stat=p.stat();p.write_bytes(p.read_bytes().replace(b',5',b',6'));os.utime(p,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    with pytest.raises(ValueError):run_experiment(c,out,resume=True)


def test_warmed_bounded_evidence_query_does_not_hash_entire_databases(tmp_path,monkeypatch):
    from test_linked_evidence import fixture
    from aiops_v4.evidence.index import query_evidence
    from aiops_v4.data import cache
    _,new=fixture(tmp_path,True)
    query_evidence(new,'stage2','states',limit=1)
    actual=cache.sha256;calls=[]
    def spy(path,end=None):
        if Path(path).suffix=='.sqlite':calls.append(str(path))
        return actual(path,end)
    monkeypatch.setattr(cache,'sha256',spy)
    query_evidence(new,'stage2','states',limit=1)
    assert calls==[]


def test_canonical_backend_preflight_happens_before_any_data_scan(tmp_path,monkeypatch):
    from test_experiment_config import config
    from aiops_v4.experiments import canonical
    from aiops_v4.experiments.run import run_experiment
    raw=tmp_path/'raw';write_csv(raw,'node_metrics.csv',[dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage='5')])
    c=config(raw);c.update(storage={'backend':'canonical','workers':1,'min_free_gib':0},backend={'kind':'http','model':'q','base_url':'http://127.0.0.1:8000','api_key_env':'MISSING_AIOPS_TEST_KEY'})
    monkeypatch.delenv('MISSING_AIOPS_TEST_KEY',raising=False)
    monkeypatch.setattr(canonical,'build_data_layers',lambda *_a,**_k:pytest.fail('scanned before backend validation'))
    with pytest.raises(ValueError,match='environment'):run_experiment(c,tmp_path/'run')


def test_canonical_resume_reuses_completed_replay_agent_stage(tmp_path,monkeypatch):
    from test_linked_evidence import fixture
    from test_experiment_config import config
    from aiops_v4.experiments import canonical
    from aiops_v4.experiments.run import run_experiment,verify_run
    from aiops_v4.agents.backend import ReplayBackend
    from test_evidence_pipeline import inputs
    raw,_,_=inputs(tmp_path,interfaces=0)
    c=config(raw);c.update(storage={'backend':'canonical','workers':1,'min_free_gib':0})
    out=tmp_path/'run';original=canonical.atomic_json
    def interrupt(path,value):
        if Path(path).name=='experiment.json':raise KeyboardInterrupt()
        return original(path,value)
    monkeypatch.setattr(canonical,'atomic_json',interrupt)
    with pytest.raises(KeyboardInterrupt):run_experiment(c,out,backend=ReplayBackend('fixture-model',[]))
    assert (out/'agents/summary.json').exists()
    monkeypatch.setattr(canonical,'atomic_json',original)
    monkeypatch.setattr(canonical,'run_diagnosis',lambda *_a,**_k:pytest.fail('completed agents repeated'))
    run_experiment(c,out,backend=ReplayBackend('fixture-model',[]),resume=True)
    assert verify_run(out)['status']=='completed'


def test_prepared_canonical_run_seal_recovers_verified_artifacts(tmp_path,monkeypatch):
    from test_experiment_config import config
    from aiops_v4.experiments import canonical
    from aiops_v4.experiments.run import run_experiment,verify_run
    raw=tmp_path/'raw';write_csv(raw,'node_metrics.csv',[dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage='5')])
    c=config(raw);c.update(agents={'discovery_only':True},storage={'backend':'canonical','workers':1,'min_free_gib':0})
    out=tmp_path/'run';original=canonical.atomic_json
    def interrupt(path,value):
        if Path(path)==out/'seal.json':raise KeyboardInterrupt()
        return original(path,value)
    monkeypatch.setattr(canonical,'atomic_json',interrupt)
    with pytest.raises(KeyboardInterrupt):run_experiment(c,out)
    monkeypatch.setattr(canonical,'atomic_json',original)
    run_experiment(c,out,resume=True)
    assert verify_run(out)['status']=='completed'

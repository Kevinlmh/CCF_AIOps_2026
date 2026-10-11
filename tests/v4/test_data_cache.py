from importlib import import_module,util
import pytest


def api():
    assert util.find_spec('aiops_v4.data.cache'), 'layer cache not implemented'
    return import_module('aiops_v4.data.cache')


def test_seal_verifies_bytes_and_rejects_unsafe_dependencies(tmp_path):
    cache=api();features=tmp_path/'features';features.mkdir();(features/'features.sqlite').write_bytes(b'payload')
    cache.seal_layer(features,{'batch':'stage1','format':'canonical_features_v1'},signature={'parser':1})
    assert cache.verify_layer(features)['batch']=='stage1'
    (features/'features.sqlite').write_bytes(b'changed')
    with pytest.raises(ValueError,match='digest'):cache.verify_layer(features)
    with pytest.raises(ValueError):cache.dependency_path(tmp_path/'states','../../external')


def test_layer_signatures_do_not_depend_on_model_and_invalidate_real_producer_changes():
    cache=api();base={'batch':'stage1','naive_timezone':'UTC','features':{'window_seconds':60},'states':{'stat_threshold':8},'evidence':{'context_seconds':120},'backend':{'model':'a'}}
    changed=dict(base,backend={'model':'b'})
    assert cache.layer_signature('features',base,[])==cache.layer_signature('features',changed,[])
    assert cache.layer_signature('states',base,['f'])==cache.layer_signature('states',changed,['f'])
    assert cache.layer_signature('states',base,['f'])!=cache.layer_signature('states',dict(base,states={'stat_threshold':9}),['f'])
    assert cache.layer_signature('features',base,[])!=cache.layer_signature('features',dict(base,naive_timezone='Asia/Shanghai'),[])


def test_canonical_experiment_reuses_data_across_model_settings_without_rescanning(tmp_path,monkeypatch):
    from test_evidence_pipeline import inputs
    from test_experiment_config import config
    from aiops_v4.experiments.run import run_experiment,verify_run
    from aiops_v4.features import build
    raw,_,_=inputs(tmp_path,interfaces=0);c=config(raw)
    c['agents']={'discovery_only':True}
    c['storage']={'backend':'canonical','workers':1,'min_free_gib':0,'data_dir':str(tmp_path/'dataset')}
    first=run_experiment(c,tmp_path/'first')
    assert first['stages']['features']['csv_parse_passes']==1
    assert first['stages']['profile']['profile_policy']=='essential'
    def forbidden(*args,**kwargs):raise AssertionError('compatible features reparsed')
    monkeypatch.setattr(build,'parse_file',forbidden)
    c['backend']['model']='different-offline-model'
    second=run_experiment(c,tmp_path/'second')
    assert first['data_layers']['features']==second['data_layers']['features']
    assert verify_run(tmp_path/'second')['status']=='completed'


def test_resource_scan_tolerates_journal_removed_by_another_worker(tmp_path,monkeypatch):
    from pathlib import Path
    from aiops_v4.data.resources import ResourcePolicy
    p=tmp_path/'file.sqlite-journal';p.write_bytes(b'temporary')
    original=Path.stat;calls=[]
    def disappear(path,*args,**kwargs):
        if path==p:
            calls.append(None)
            if len(calls)==2:p.unlink()
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'stat',disappear)
    policy=ResourcePolicy(tmp_path,min_free_bytes=0)
    policy.check()


def test_long_sql_checks_workspace_and_keeps_temp_files_in_owned_directory(tmp_path,monkeypatch):
    import sqlite3
    from aiops_v4.data.resources import ResourcePolicy,WorkspaceBudgetError
    policy=ResourcePolicy(tmp_path,min_free_bytes=0,max_workspace_bytes=1024*1024)
    original=policy.check;calls=[]
    def fill():
        calls.append(None)
        if len(calls)==2:(tmp_path/'large.private').write_bytes(b'x'*(2*1024*1024))
        return original()
    monkeypatch.setattr(policy,'check',fill)
    with sqlite3.connect(':memory:') as db:
        with pytest.raises(WorkspaceBudgetError):
            with policy.guard_sql(db):
                temp=__import__('pathlib').Path(db.execute('PRAGMA temp_store_directory').fetchone()[0])
                assert temp==tmp_path/'.sqlite-temp'
                db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n').fetchone()
    assert len(calls)>=2


def test_feature_signature_includes_raw_root_used_for_city_inference():
    cache=api();base=dict(batch='s',raw_root='/input/xian',naive_timezone='UTC',features={})
    changed=dict(base,raw_root='/input/beida')
    assert cache.layer_signature('features',base,[])!=cache.layer_signature('features',changed,[])

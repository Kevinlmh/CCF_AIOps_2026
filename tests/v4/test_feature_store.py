import json
import math
from importlib import import_module, util
import pytest
from test_records import write_csv
from aiops_v4.features.pipeline import build_features


def api():
    assert util.find_spec('aiops_v4.features.build'), 'canonical feature builder not implemented'
    return import_module('aiops_v4.features.build'),import_module('aiops_v4.features.store')


def compare(a,b):
    if isinstance(a,dict):
        assert a.keys()==b.keys()
        for k in a: compare(a[k],b[k])
    elif isinstance(a,list):
        assert len(a)==len(b)
        for x,y in zip(a,b): compare(x,y)
    elif isinstance(a,float): assert math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-12)
    else: assert a==b


def fixture(root):
    base=dict(node='br1')
    write_csv(root,'node_metrics_b.csv',[dict(base,timestamp='2026-09-17T04:01:00Z',cpu_usage='0',load1='Infinity')])
    write_csv(root,'node_metrics_a.csv',[dict(base,timestamp='2026-09-17T04:00:00Z',cpu_usage='0',load1=''),
                                       dict(base,timestamp='2026-09-17T04:00:00Z',cpu_usage='1',load1='bad')])
    peer=dict(node='br1',metric_name='test_total',label='peer="a"')
    write_csv(root,'routing_metrics_a.csv',[dict(peer,timestamp='2026-09-17T04:00:30Z',value='100')])
    write_csv(root,'routing_metrics_b.csv',[dict(peer,timestamp='2026-09-17T04:01:30Z',value='160')])
    flow=dict(node_key='br1',interface_id='ens4',protocol='6')
    write_csv(root,'netflow_a.csv',[dict(flow,minute_utc='2026-09-17T04:01:00Z',bytes='7',packets='-1'),
                                  dict(flow,minute_utc='2026-09-17T04:00:00Z',bytes='5',packets='')])
    write_csv(root,'netflow_b.csv',[dict(flow,minute_utc='2026-09-17T04:00:00Z',bytes=str(i),packets='2') for i in range(30)])


@pytest.mark.parametrize('workers',[1,2])
def test_canonical_windows_equal_legacy_including_cross_file_counter_and_netflow(tmp_path,workers):
    raw=tmp_path/'raw';fixture(raw)
    old=tmp_path/'old';build_features(raw,'stage1',old,naive_timezone='UTC',netflow_streaming=True)
    expected=[json.loads(l) for l in (old/'windows.jsonl').read_text().splitlines()]
    build,store=api();out=tmp_path/'new'
    summary=build.build_feature_store(raw,'stage1',out,naive_timezone='UTC',workers=workers,min_free_bytes=0)
    assert summary['profile_policy']=='essential' and summary['csv_parse_passes']==1
    assert summary['rows_scanned']==sum(f['rows_scanned'] for f in summary['files'])
    assert not (out/'windows.jsonl').exists()
    with store.FeatureStore(out/'features.sqlite') as db:
        actual=list(db.windows())
        compare(expected,actual)
        for window in actual:
            for ref in window['references']:
                assert db.raw_record(ref['record_id'])['record_id']==ref['record_id']
    counter=next(w for w in actual if w['source']=='routing' and w['start_time'].endswith('04:01:00.000000Z'))
    assert counter['metrics']['test_total']['delta_sum']==60
    assert counter['metrics']['test_total']['rate_mean']==1
    assert len(counter['references'])==2


def test_interruption_resumes_committed_file_chunk_without_reparsing_rows(tmp_path):
    raw=tmp_path/'raw';fixture(raw);build,store=api();out=tmp_path/'new'
    seen=[]
    def interrupt(v):
        seen.append(v)
        if v.get('event')=='parse_checkpoint':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        build.build_feature_store(raw,'stage1',out,naive_timezone='UTC',workers=1,min_free_bytes=0,
                                  checkpoint_rows=2,progress=interrupt)
    assert not (out/'summary.json').exists()
    notifications=[]
    summary=build.build_feature_store(raw,'stage1',out,naive_timezone='UTC',workers=1,min_free_bytes=0,
                                     checkpoint_rows=2,resume=True,progress=notifications.append)
    assert summary['rows_scanned']==37
    assert any(v.get('event')=='parse_resumed' and v.get('record_index',0)>=2 for v in notifications)
    with store.FeatureStore(out/'features.sqlite') as db:
        assert sum(w['record_count'] for w in db.windows())==37


def test_workspace_ceiling_and_changed_input_refuse_to_publish(tmp_path):
    raw=tmp_path/'raw';fixture(raw);build,_=api();out=tmp_path/'budget'
    with pytest.raises(RuntimeError,match='workspace'):
        build.build_feature_store(raw,'stage1',out,naive_timezone='UTC',workers=1,min_free_bytes=0,max_workspace_bytes=1)
    assert not (out/'summary.json').exists()
    p=next(raw.rglob('node_metrics_a.csv'));p.write_text(p.read_text()+'\n')
    with pytest.raises(ValueError,match='changed'):
        build.build_feature_store(raw,'stage1',out,naive_timezone='UTC',workers=1,min_free_bytes=0,resume=True,max_workspace_bytes=1)


def test_statistics_codec_preserves_unknown_metadata_missing_zero_and_integer_counts():
    _,store=api()
    value={'count':7,'mean':0.0,'median':None,'quantiles_exact':True,'new_field':{'unit':'unknown'},'overflowed_statistics':['std'],'std':None}
    blob,metadata=store.pack_statistics(value)
    assert store.unpack_statistics(blob,metadata)==value
    assert isinstance(blob,bytes)


def test_feature_store_interns_extras_and_uses_typed_anchor_positions(tmp_path):
    raw=tmp_path/'raw';fixture(raw);build,store=api();out=tmp_path/'new'
    build.build_feature_store(raw,'stage1',out,naive_timezone='UTC',workers=1,min_free_bytes=0)
    with store.FeatureStore(out/'features.sqlite') as f:
        assert f.db.execute('PRAGMA user_version').fetchone()[0]==4
        assert f.db.execute('SELECT count(*) FROM extras').fetchone()[0]<f.summary['window_count']
        assert all(length==40 for length, in f.db.execute('SELECT length(payload) FROM anchors'))


def test_parallel_native_aggregation_uses_workers_and_resumes_completed_groups(tmp_path):
    import os
    raw=tmp_path/'raw';fixture(raw);build,_=api();out=tmp_path/'new';events=[]
    def interrupt(value):
        events.append(value)
        if value.get('event')=='group_complete':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):build.build_feature_store(raw,'stage1',out,naive_timezone='UTC',workers=2,min_free_bytes=0,progress=interrupt)
    assert any(e.get('aggregation_worker_pid') not in (None,os.getpid()) for e in events)
    resumed=[]
    result=build.build_feature_store(raw,'stage1',out,naive_timezone='UTC',workers=2,min_free_bytes=0,resume=True,progress=resumed.append)
    assert result['rows_scanned']==37 and any(e.get('event')=='group_reused' for e in resumed)

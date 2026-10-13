"""Real linked query plans must search the canonical identifiers and grains."""
from pathlib import Path
import sqlite3
import json
import os
import pytest

from test_linked_evidence import fixture,strip_provenance
from test_feature_store import compare
from aiops_v4.evidence import index


@pytest.mark.parametrize('skip_raw',[False,True])
def test_linked_raw_registration_uses_digest_index(tmp_path,monkeypatch,skip_raw):
    old,new=fixture(tmp_path,skip_raw)
    reference=index.query_evidence(new,'stage2','bundle')[0]['references'][0]
    expected=index.query_raw(old,'stage2',reference=reference)
    actual_connect=sqlite3.connect;plans=[]

    class LookupConnection(sqlite3.Connection):
        def execute(self,sql,parameters=(),/):
            if sql.startswith('SELECT') and ('FROM anchors WHERE id=' in sql or 'FROM upstream_features.anchors' in sql):
                plan=list(super().execute('EXPLAIN QUERY PLAN '+sql,parameters));plans.append(plan)
                assert any('SEARCH a' in row[3] and 'digest=?' in row[3] for row in plan),plan
                assert not any('SCAN a' in row[3] for row in plan),plan
            return super().execute(sql,parameters)

    def connect(database,*args,**kwargs):
        if str(database).split('?')[0].endswith('/evidence.sqlite'):kwargs['factory']=LookupConnection
        return actual_connect(database,*args,**kwargs)

    monkeypatch.setattr(index.sqlite3,'connect',connect)
    compare(expected,index.query_raw(new,'stage2',reference=reference))
    assert len(plans)==1


def test_linked_context_searches_native_group_and_time_ranges(tmp_path,monkeypatch):
    actual_connect=sqlite3.connect;plans=[]

    class ContextConnection(sqlite3.Connection):
        def execute(self,sql,parameters=(),/):
            context_query=sql.startswith('SELECT') and ('FROM states' in sql or 'upstream_states.deltas' in sql)
            if context_query:
                plan=list(super().execute('EXPLAIN QUERY PLAN '+sql,parameters));plans.append(plan)
                assert not any('SCAN d' in row[3] or 'SCAN upstream_states.deltas' in row[3] for row in plan),plan
                assert any('SEARCH d' in row[3] or 'SEARCH upstream_states.deltas' in row[3] for row in plan),plan
            return super().execute(sql,parameters)

    def connect(database,*args,**kwargs):
        if str(database).split('?')[0].endswith('/.evidence.sqlite'):kwargs['factory']=ContextConnection
        return actual_connect(database,*args,**kwargs)

    monkeypatch.setattr(index.sqlite3,'connect',connect)
    old,new=fixture(tmp_path,True)
    compare(strip_provenance(index.query_evidence(old,'stage2','bundle')),strip_provenance(index.query_evidence(new,'stage2','bundle')))
    assert len(plans)>=14


def test_linked_raw_lookup_preserves_registration_and_content_guards(tmp_path):
    _,new=fixture(tmp_path,True)
    row=index.query_evidence(new,'stage2','states',entity_id='xian-br-1',source='routing',
        start='2026-09-17T04:12:00Z',end='2026-09-17T04:13:00Z')[0]
    reference=row['references'][0]
    assert index.query_raw(new,'stage2',reference=reference)['record_id']==reference['record_id']
    for changed in (dict(reference,line_end=reference['line_end']+1),dict(reference,source_file='../outside.csv')):
        with pytest.raises(ValueError):index.query_raw(new,'stage2',reference=changed)
    for identifier in (reference['record_id'].upper(),'0'*64,'not-a-digest'):
        with pytest.raises(ValueError):index.query_raw(new,'stage2',identifier)
    with pytest.raises(ValueError):index.query_raw(new,'other',reference=reference)
    path=tmp_path/'raw'/reference['source_file'];before=path.stat()
    data=path.read_bytes();lines=data.splitlines(keepends=True)
    position=reference['line_start']-1
    changed=lines[position].replace(b',0',b',1')
    assert changed!=lines[position] and len(changed)==len(lines[position])
    lines[position]=changed;path.write_bytes(b''.join(lines));os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    with pytest.raises(ValueError,match='content ID mismatch'):
        index.query_raw(new,'stage2',reference=reference)


def test_native_context_matches_view_context_for_suppression_boundaries_and_cities(tmp_path):
    from test_records import write_csv
    from aiops_v4.features.build import build_feature_store
    from aiops_v4.states.build import build_state_store
    from aiops_v4.states.store import StateStore
    from aiops_v4.evidence import linked
    from aiops_v4.evidence.pipeline import _context
    from aiops_v4.storage import load_json
    raw=tmp_path/'raw';node=[];routing=[]
    for role in ('br1','br2'):
        for i in range(15):
            stamp=f'2026-09-17T04:{i:02d}:00Z'
            node.append(dict(timestamp=stamp,node=role,cpu_usage='90' if i==12 else '10'))
            routing.append(dict(timestamp=stamp,node=role,metric_name='bgp_peer_up',label='peer="192.0.2.1"',value='0' if i in (12,13) else '1'))
    write_csv(raw,'node_metrics.csv',node);write_csv(raw,'routing_metrics.csv',routing)
    write_csv(raw,'scrape_health.csv',[dict(timestamp='2026-09-17T04:12:00Z',node='br1',target_id='test:9100',exporter_type='node',scrape_up='1')])
    other=write_csv(tmp_path/'other','node_metrics.csv',node)
    target=raw/'beida/processed/node_metrics.csv';target.parent.mkdir(parents=True);target.write_bytes(other.read_bytes())
    dataset=tmp_path/'dataset';features=dataset/'features';states=dataset/'states';evidence=dataset/'evidence'
    build_feature_store(raw,'stage2',features,naive_timezone='UTC',workers=1,min_free_bytes=0)
    build_state_store(features,'stage2',states,candidate_policy='conservative',reference_start='2026-09-17T04:00:00Z',
        reference_end='2026-09-17T04:12:00Z',workers=1,min_free_bytes=0)
    topology=tmp_path/'topology.json';topology.write_text(json.dumps(dict(provenance='fixture',edges=[dict(a='xian-br-1',b='xian-br-2',relation='link')])))
    linked.build_linked_evidence(states,'stage2',evidence,skip_raw=True,topology=topology,min_free_bytes=0)
    with index.read_index(evidence/'evidence.sqlite','stage2') as db,StateStore(states/'states.sqlite') as state:
        state_summary=index.metadata(db,'state_summary');window_summary=index.metadata(db,'window_summary');topology=index.metadata(db,'topology')
        assert db.execute('SELECT count(*) FROM upstream_states.deltas WHERE suppressed=1').fetchone()[0]>=2
        assert any(entity.startswith('beida-') for entity, in db.execute('SELECT DISTINCT entity FROM upstream_features.groups'))
        bundles=[load_json(p) for p, in db.execute('SELECT payload FROM bundles')]
        assert len(bundles)==2
        for bundle in bundles:
            for seconds in (0,1,60,120,86400):
                for cap in (1,2,8,32):
                    expected=_context(db,bundle,seconds,cap,state_summary,window_summary,topology,True)
                    actual=linked._linked_context(db,state,bundle,seconds,cap,state_summary,window_summary,topology,True)
                    compare(expected,actual)
                    assert all(observation['identity']['entity_id']==bundle['entity_id'] for kind in
                        ('support','observations_without_current_trigger','quality_context') for observation in actual[kind])

import json
from importlib import import_module,util
from pathlib import Path
import shutil
import sqlite3
import pytest
from test_evidence_pipeline import inputs
from test_feature_store import compare
from aiops_v4.features.build import build_feature_store
from aiops_v4.states.build import build_state_store
from aiops_v4.evidence.pipeline import build_evidence
from aiops_v4.evidence.index import query_evidence,query_raw


def fixture(tmp_path,skip_raw):
    raw,_,states=inputs(tmp_path,interfaces=3);old=tmp_path/'legacy';build_evidence(states,'stage2',old,skip_raw=skip_raw,observations_per_kind=2)
    dataset=tmp_path/'dataset';features=dataset/'features'
    build_feature_store(raw,'stage2',features,naive_timezone='UTC',workers=1,min_free_bytes=0,netflow_streaming=False)
    build_state_store(features,'stage2',dataset/'states',reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z',workers=1,min_free_bytes=0)
    assert util.find_spec('aiops_v4.evidence.linked'), 'linked evidence backend not implemented'
    linked=import_module('aiops_v4.evidence.linked');new=dataset/'evidence'
    linked.build_linked_evidence(dataset/'states','stage2',new,skip_raw=skip_raw,observations_per_kind=2,min_free_bytes=0)
    return old/'evidence.sqlite',new/'evidence.sqlite'


def strip_provenance(row):
    if isinstance(row,dict):return {k:strip_provenance(v) for k,v in row.items() if k!='scoring_provenance'}
    if isinstance(row,list):return [strip_provenance(v) for v in row]
    return row


@pytest.mark.parametrize('skip_raw',[False,True])
def test_tool_pages_packets_members_models_and_raw_match_legacy_without_full_copy(tmp_path,skip_raw):
    old,new=fixture(tmp_path,skip_raw)
    for kind in ('bundle','states'):
        for offset in (0,1,5,60):
            compare(strip_provenance(query_evidence(old,'stage2',kind,limit=5,offset=offset)),strip_provenance(query_evidence(new,'stage2',kind,limit=5,offset=offset)))
    for packet in query_evidence(old,'stage2','bundle'):
        for kind in ('members','relations'):
            compare(strip_provenance(query_evidence(old,'stage2',kind,bundle_id=packet['bundle_id'])),strip_provenance(query_evidence(new,'stage2',kind,bundle_id=packet['bundle_id'])))
        for ref in packet['references']:
            a=query_raw(old,'stage2',reference=ref);b=query_raw(new,'stage2',reference=ref)
            compare(a,b)
    states=query_evidence(old,'stage2','states',entity_id='xian-br-1',source='node',start='2026-09-17T04:12:00Z',end='2026-09-17T04:13:00Z')
    compare(strip_provenance(states),strip_provenance(query_evidence(new,'stage2','states',entity_id='xian-br-1',source='node',start='2026-09-17T04:12:00Z',end='2026-09-17T04:13:00Z')))
    for row in states:
        compare(strip_provenance(query_evidence(old,'stage2','model',group_id=row['group_id'])),strip_provenance(query_evidence(new,'stage2','model',group_id=row['group_id'])))
    with sqlite3.connect(new) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables & {'states','matrices','models','anchors'}
    moved=tmp_path/'moved';shutil.move(new.parent.parent,moved)
    assert len(query_evidence(moved/'evidence/evidence.sqlite','stage2','bundle'))==2


def test_agent_candidate_preparation_matches_legacy_on_linked_views(tmp_path):
    from aiops_v4.agents.contracts import load_manifest
    from aiops_v4.agents.tools import EvidenceSession
    old,new=fixture(tmp_path,True)
    packet=query_evidence(old,'stage2','bundle')[0]
    ids=[r['vector_id'] for r in load_manifest(old,'stage2',packet['bundle_id'])]
    a=EvidenceSession(old,'stage2',packet);b=EvidenceSession(new,'stage2',query_evidence(new,'stage2','bundle')[0])
    a.prepare_candidates(ids);b.prepare_candidates(ids)
    compare(strip_provenance(a.packet),strip_provenance(b.packet))


def test_linked_join_uses_index_for_matrix_lookup(tmp_path):
    from aiops_v4.evidence.index import read_index
    _,new=fixture(tmp_path,True)
    with read_index(new,'stage2') as db:
        plan=list(db.execute('''EXPLAIN QUERY PLAN SELECT s.payload,x.payload
            FROM states s JOIN matrices x ON x.id=s.id WHERE s.entity=?
            ORDER BY s.start,s.source,s.grp,s.id LIMIT 8''',('xian-br-1',)))
        # The matrix side must look up each selected ID, not scan all deltas
        # for every window in the entity. No assumptions about plan node numbers.
        assert not any('SCAN upstream_states.deltas' in row[3] for row in plan),plan
        assert any('SEARCH upstream_states.deltas' in row[3] for row in plan),plan

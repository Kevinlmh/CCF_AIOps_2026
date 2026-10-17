import importlib.util
from pathlib import Path
import pytest
P=Path(__file__).resolve().parents[2]/'scripts/v4_a1_pilot.py'
def mod():
 s=importlib.util.spec_from_file_location('a1pilot',P);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def b(i,city,start,end,targets):
 return dict(bundle_id=i,entity_id=city+'-traffic-vm',start_time='2026-08-19T00:'+start+':00+00:00',end_time='2026-08-19T00:'+end+':00+00:00',support=[dict(dimensions=dict(flow_type=x[0],target_region=x[1],target_domain=x[2])) for x in targets])
K=('dns','beida','dns.beida.aiops.local')
def test_cross_city_same_target_merges_and_keeps_all_ids():
 m=mod();g=m.cluster([b('a','xian','00','03',[K]),b('b','wuhan','04','06',[K])])
 assert len(g)==1 and sorted(x['bundle_id'] for x in g[0]['packets'])==['a','b']
 assert g[0]['side']=='target'
def test_different_target_and_gap_do_not_merge():
 m=mod();other=('web','shanghai','web01.shanghai.aiops.local')
 assert len(m.cluster([b('a','xian','00','03',[K]),b('b','wuhan','00','03',[other])]))==2
 assert len(m.cluster([b('a','xian','00','03',[K]),b('b','wuhan','06','07',[K])]))==2
def test_multi_target_is_not_a_bridge_between_services():
 m=mod();other=('web','shanghai','web01.shanghai.aiops.local')
 g=m.cluster([b('a','xian','00','03',[K]),b('b','wuhan','00','03',[K,other]),b('c','beida','00','03',[other])])
 assert len(g)==3 and any(x['side']=='ambiguous' for x in g)
 assert {p['bundle_id'] for x in g for p in x['packets']}=={'a','b','c'}
def test_endpoint_mapping_is_candidate_not_diagnosis():
 m=mod();v=m.endpoint_hint(K)
 assert v['network_element_id']=='beida-service-vm-1' and v['causal_root_confirmed'] is False
 assert m.endpoint_hint(('dns','shanghai','dns.beida.aiops.local')) is None
def test_deterministic_ids_and_resource_units_preserved():
 m=mod();x=b('a','xian','00','03',[K]);y=b('b','wuhan','00','03',[K])
 assert m.cluster([x,y])[0]['id']==m.cluster([y,x])[0]['id']
def test_missing_targets_not_dropped():
 m=mod();x=b('a','xian','00','03',[])
 assert m.cluster([x])[0]['side']=='ambiguous'

def test_bounded_observation_selection_keeps_target_and_peer_nodes():
 m=mod();rows=[dict(vector_id='i'+str(i),entity_id='city'+str(i%8)+'-traffic-vm',source='interface') for i in range(70)]
 nodes=[dict(vector_id='n'+str(i),entity_id='target-service-vm-'+str(i%3+1),source='node') for i in range(9)]
 result=m.select_observations(rows+nodes,32,priority_entities={r['entity_id'] for r in nodes})
 assert len(result)==32 and {r['vector_id'] for r in nodes}.issubset({r['vector_id'] for r in result})
 assert len({r['entity_id'] for r in result if r['source']=='interface'})==8
 assert m.select_observations(rows+nodes,32,priority_entities={r['entity_id'] for r in nodes})==result

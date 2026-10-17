"""Reduced model protocol keeps explicit grouping and binds each root to its own proof."""
from aiops_v4.agents.structured import canonical_result,result_tool
from aiops_v4.agents.jsonio import dumps
import pytest
def payload():
 return dict(bundle=dict(initial_evidence_profile='a1-readable-1',a1_role_contract='simple-v1'),manifest=[dict(vector_id='wA'),dict(vector_id='wB')])
def assessment():
 return dict(decision='confirmed',confidence=.8,reason='measured change',citations=[],missing_evidence=[])
def test_single_assessment_all_is_explicit_and_has_no_duplicate_group_identifier():
 p=payload();r=canonical_result('confirmation',dict(assignments='all',assessments=[assessment()]),p)
 assert r['assessments'][0]['window_ids']==['wA','wB']
 schema=result_tool('confirmation',p,{})['function']['parameters']
 assert 'anyOf' in schema
 assert 'group_id' not in dumps(schema)
def test_multiple_assessments_are_bound_by_position_and_must_own_windows():
 p=payload();r=canonical_result('confirmation',dict(assignments={'w0':'a0','w1':'a1'},assessments=[assessment(),assessment()]),p)
 assert [a['window_ids'] for a in r['assessments']]==[['wA'],['wB']]
 with pytest.raises(ValueError):canonical_result('confirmation',dict(assignments='all',assessments=[assessment(),assessment()]),p)
 with pytest.raises(ValueError):canonical_result('confirmation',dict(assignments={'w0':'a0','w1':'a0'},assessments=[assessment(),assessment()]),p)
def test_each_candidate_schema_binds_support_to_the_same_device():
 p=payload();seen={('state','a'):{'vector_id':'a','entity_id':'beida-service-vm-1','source':'node'},
 ('state','b'):{'vector_id':'b','entity_id':'beida-service-vm-2','source':'node'}}
 schema=result_tool('localization',p,seen)['function']['parameters']
 resolved=next(v for v in schema['anyOf'] if v['properties']['decision']['enum']==['resolved'])
 variants=resolved['properties']['candidates']['items']['anyOf']
 for v in variants:
  device=v['properties']['network_element_id']['enum'][0]
  ids={i for r in v['properties']['support']['anyOf'] for i in r['properties']['id']['enum']}
  assert ids==({'a'} if device=='beida-service-vm-1' else {'b'})
def test_simple_localization_is_explicitly_converted_to_canonical_proof():
 p=payload()
 raw=dict(decision='resolved',candidates=[dict(network_element_id='beida-service-vm-1',confidence=.8,reason='reason',
  support=dict(kind='state',id='a',claim='own measured state'),counter_citations=[])],reason='reason',missing_evidence=[])
 r=canonical_result('localization',raw,p)
 assert r['candidates'][0]['citations']==[dict(kind='state',id='a',claim='own measured state',stance='support')]
 assert 'support' not in r['candidates'][0]

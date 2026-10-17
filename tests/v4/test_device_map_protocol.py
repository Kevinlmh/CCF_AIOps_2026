"""One root entry per device, with multiple proofs and unchanged canonical validation."""
import pytest
from aiops_v4.agents.structured import canonical_result,result_tool,validate_result
from aiops_v4.agents.jsonio import loads

def payload():
 return dict(bundle=dict(initial_evidence_profile='a1-readable-1',a1_role_contract='simple-v2'))
def seen():
 return {('state','cpu'):dict(entity_id='wuhan-service-vm-1',source='node'),
         ('state','disk'):dict(entity_id='wuhan-service-vm-1',source='node'),
         ('state','peer'):dict(entity_id='wuhan-service-vm-2',source='node')}
def proof(identifier):
 return dict(kind='state',id=identifier,claim='own observed metric')
def entry(priority=1):
 return dict(priority=priority,confidence=.8,reason='multiple measured changes',support=[proof('cpu'),proof('disk')],counter_citations=[])
def raw():
 return dict(decision='resolved',candidates_by_device={'wuhan-service-vm-1':entry()},reason='own measurements',missing_evidence=[])
def test_multiple_metrics_stay_one_root_and_original_validation_applies():
 r=canonical_result('localization',raw(),payload())
 assert len(r['candidates'])==1
 assert len(r['candidates'][0]['citations'])==2
 validate_result('localization',r,payload(),seen())
def test_device_keys_are_schema_bound_to_own_proofs():
 schema=result_tool('localization',payload(),seen())['function']['parameters']
 resolved=next(v for v in schema['anyOf'] if v['properties']['decision']['enum']==['resolved'])
 devices=resolved['properties']['candidates_by_device']
 assert devices['additionalProperties'] is False
 assert set(devices['properties'])=={'wuhan-service-vm-1','wuhan-service-vm-2'}
 for device,item in devices['properties'].items():
  identifiers={i for alt in item['properties']['support']['items']['anyOf'] for i in alt['properties']['id']['enum']}
  assert identifiers==({'cpu','disk'} if device.endswith('-1') else {'peer'})
def test_wrong_device_support_still_rejected():
 x=raw();x['candidates_by_device']['wuhan-service-vm-1']['support']=[proof('peer')]
 with pytest.raises(ValueError,match='own observed support'):
  validate_result('localization',canonical_result('localization',x,payload()),payload(),seen())
def test_model_priority_determines_order_and_abstention_is_explicit():
 x=raw();x['candidates_by_device']['wuhan-service-vm-1']['priority']=2
 x['candidates_by_device']['wuhan-service-vm-2']=dict(entry(1),support=[proof('peer')])
 r=canonical_result('localization',x,payload())
 assert [c['network_element_id'] for c in r['candidates']]==['wuhan-service-vm-2','wuhan-service-vm-1']
 d=dict(decision='deferred',candidates_by_device={},reason='not enough causal evidence',missing_evidence=['mechanism absent'])
 assert canonical_result('localization',d,payload())['candidates']==[]
def test_duplicate_device_json_cannot_be_silently_collapsed():
 with pytest.raises(ValueError,match='duplicate JSON key'):
  loads('{"wuhan-service-vm-1":{},"wuhan-service-vm-1":{}}')

def test_all_supports_must_belong_to_the_selected_device():
 x=raw();x['candidates_by_device']['wuhan-service-vm-1']['support']=[proof('cpu'),proof('peer')]
 with pytest.raises(ValueError,match='every root support'):
  validate_result('localization',canonical_result('localization',x,payload()),payload(),seen())
def test_v2_prompt_has_one_format_and_no_legacy_root_array():
 from aiops_v4.agents.structured import role_system_prompt
 prompt=role_system_prompt('localization',payload(),True)
 assert 'candidates_by_device' in prompt
 assert '"candidates":[' not in prompt
 assert 'assignments={' not in prompt

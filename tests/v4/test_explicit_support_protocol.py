"""Confirmed/resolved/accepted decisions explicitly carry positive event evidence."""
import pytest
from aiops_v4.agents.structured import result_tool,canonical_result,validate_result
def payload():
 return dict(bundle=dict(initial_evidence_profile='a1-readable-1',a1_role_contract='simple-v2'),
  manifest=[dict(vector_id='own',event_id='e',start_time='2026-08-20T00:00:00Z',end_time='2026-08-20T00:01:00Z')],
  event=dict(window_ids=['own']))
def seen():
 return {('state','own'):dict(entity_id='wuhan-service-vm-1',source='node'),
         ('state','outside'):dict(entity_id='wuhan-service-vm-1',source='node')}
def proof(identifier='own'):
 return dict(kind='state',id=identifier,claim='observed change')
@pytest.mark.parametrize('role,field,decision',[('confirmation','assessments','confirmed'),('classification',None,'resolved'),('review',None,'accept')])
def test_positive_schema_requires_support_not_just_counter(role,field,decision):
 schema=result_tool(role,payload(),seen())['function']['parameters']
 if field:schema=schema['anyOf'][0]['properties'][field]['items']
 positive=next(v for v in schema['anyOf'] if v['properties']['decision']['enum']==[decision])
 assert positive['properties']['support']['minItems']==1
 ids={i for v in positive['properties']['support']['items']['anyOf'] for i in v['properties']['id']['enum']}
 assert ids=={'own'}
 assert 'counter_citations' in positive['properties']
def test_confirmation_support_converts_and_counter_only_stays_invalid():
 item=dict(decision='confirmed',confidence=.8,reason='observed change',support=[proof()],mechanism_support=[],counter_citations=[],missing_evidence=[])
 raw=dict(assignments='all',assessments=[item])
 out=canonical_result('confirmation',raw,payload())
 assert out['assessments'][0]['citations'][0]['stance']=='support'
 validate_result('confirmation',out,payload(),seen())
 item['support']=[];item['counter_citations']=[dict(proof(),stance='counter')]
 with pytest.raises(ValueError,match='observed support'):
  validate_result('confirmation',canonical_result('confirmation',raw,payload()),payload(),seen())
def test_classification_and_review_keep_evidence_rules_after_conversion():
 c=dict(decision='resolved',category=dict(major_category='resource',sub_category='cpu_pressure'),confidence=.8,
  reason='observed change',support=[proof()],mechanism_support=[],counter_citations=[],missing_evidence=[])
 validate_result('classification',canonical_result('classification',c,payload()),payload(),seen())
 r=dict(decision='accept',reason='observed change',support=[proof()],mechanism_support=[],counter_citations=[],missing_evidence=[],
  counterevidence_assessment='outside observation does not establish health',contradictions=[])
 validate_result('review',canonical_result('review',r,payload()),payload(),seen())
 c['support']=[proof('outside')]
 with pytest.raises(ValueError,match='this confirmed event'):
  validate_result('classification',canonical_result('classification',c,payload()),payload(),seen())

def test_mechanism_proof_can_be_outside_candidate_scope_without_becoming_counter():
 c=dict(decision='resolved',category=dict(major_category='resource',sub_category='cpu_pressure'),confidence=.8,
  reason='event symptom plus separately observed mechanism',support=[proof()],
  mechanism_support=[proof('outside')],counter_citations=[],missing_evidence=[])
 out=canonical_result('classification',c,payload())
 assert [(r['id'],r['stance']) for r in out['citations']]==[('own','support'),('outside','support')]
 validate_result('classification',out,payload(),seen())

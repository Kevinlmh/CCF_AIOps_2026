"""A1 initial summaries are bounded; complete stores and tool access remain intact."""
import copy
import pytest
from aiops_v4.agents import tools

def counter():
 return dict(vector_id='s',source='traffic',entity_id='xian-traffic-vm',
  rule_inputs={'requests':dict(kind='counter',unit='count',usable=True,mean=999,last=1000,min=998,max=1000,change_count=2,
                              delta_sum=2,rate_mean=.1,counter_decrease_count=0,counter_gap_count=1)},
  feature_values={'error_ratio::mean':dict(raw_value=.7,unit='ratio',semantic_status='registered')})
def test_a1_initial_counter_summary_preserves_rates_resets_and_missingness():
 x=counter();before=copy.deepcopy(x)
 row=tools.compact_initial_observation(x,profile='a1-bounded-1');r=row['rule_inputs']['requests']
 assert all(k not in r for k in ('mean','last','min','max','change_count'))
 assert r['delta_sum']==2 and r['rate_mean']==.1 and r['rate_unit']=='count/second'
 assert r['counter_decrease_count']==0 and r['counter_gap_count']==1 and r['usable'] is True
 assert row['feature_values']['error_ratio::mean']['raw_value']==.7
 assert 'cumulative' in row['rule_input_projection']
 assert x==before
def test_default_observation_and_explicit_tool_details_remain_full():
 x=counter();row=tools.compact_initial_observation(x)
 assert row==tools.compact_role_observation(x)
 assert row['rule_inputs']['requests']['max']==1000
def test_candidate_sample_covers_groups_and_ends_when_capacity_allows():
 rows=[dict(vector_id=str(i)+'-'+str(j),group_id='g'+str(i),start_time=str(j).zfill(3)) for i in range(4) for j in range(10)]
 selected=tools.sample_candidate_ids(rows,{r['vector_id'] for r in rows},8)
 assert set(selected)=={str(i)+'-'+str(j) for i in range(4) for j in (0,9)}
 assert len(selected)==len(set(selected))
def test_a1_sampling_records_smaller_initial_budget(evidence):
 from aiops_v4.agents.contracts import load_manifest
 db,packet=evidence;packet=dict(packet,initial_evidence_profile='a1-bounded-1')
 session=tools.EvidenceSession(db,'stage2',packet)
 manifest=load_manifest(db,'stage2',packet['bundle_id'])
 session.prepare_candidates([w['vector_id'] for w in manifest])
 assert session.packet['initial_evidence_delivery']['candidate_observation_cap']==8
 assert session.packet['initial_evidence_delivery']['complete_candidate_manifest_retained'] is True
from test_agent_tools import evidence

def test_initial_comparison_sample_keeps_first_and_last_with_explicit_cap():
 rows=[dict(vector_id=str(i),start_time=str(i)) for i in range(8)]
 assert tools.sample_comparison_observations(rows,2)==[rows[0],rows[-1]]
 assert tools.sample_comparison_observations(rows[:1],2)==rows[:1]
 assert len(rows)==8

def test_readable_profile_keeps_fault_measurements_and_counter_validity():
 x=counter();x['dimensions']={'flow_type':'auth'}
 x['feature_values'].update({'auth_flow_error_total::rate_mean':dict(raw_value=.4,unit='count/second',semantic_status='registered'),
  'auth_flow_bytes_sent_total::rate_mean':dict(raw_value=100,unit='bytes/second',semantic_status='registered')})
 row=tools.compact_initial_observation(x,profile='a1-readable-1')
 assert row['feature_values']['auth_flow_error_total::rate_mean']['raw_value']==.4
 assert 'auth_flow_bytes_sent_total::rate_mean' not in row['feature_values']
 assert row['counter_checks']['requests']['counter_gap_count']==1
 assert row['initial_projection']['omitted_feature_count']>=1
 assert row['counter_deltas']['requests']=={'delta_sum':2,'unit':'count'}
def test_a1_query_summary_is_complete_and_full_details_still_accessible(evidence,monkeypatch):
 db,packet=evidence;packet=dict(packet,initial_evidence_profile='a1-readable-1')
 session=tools.EvidenceSession(db,'stage2',packet);session.compact_queries=True
 state=counter();state['batch']='stage2';state['group_id']='g';state['dimensions']={'flow_type':'auth'}
 state['rule_inputs']={f'auth_flow_error_{i}_total':dict(kind='counter',unit='count',usable=True,mean=1e9+i,last=1e9+i+1,min=1e9,max=1e9+2,delta_sum=2,rate_mean=.1,counter_decrease_count=0,counter_gap_count=0) for i in range(50)}
 state['feature_values']={'auth_flow_error_total::rate_mean':dict(raw_value=.4,unit='count/second',semantic_status='registered')}
 monkeypatch.setattr(tools,'query_evidence',lambda *a,**kw:[state])
 reply=session.call('query_states',{'entity_id':'xian-traffic-vm'})
 assert reply['complete'] is True
 value=__import__('json').loads(reply['text'])[0]
 assert 'mean' not in next(iter(value['rule_inputs'].values()),{})
 full=session.call('query_states',{'entity_id':'xian-traffic-vm','detail':'full'})
 from test_agent_tools import consume
 assert consume(session,full)[0]['rule_inputs']['auth_flow_error_0_total']['mean']==1e9
def test_query_detail_option_is_only_exposed_for_opt_in_profile():
 from aiops_v4.agents.structured import role_tools
 def schema(profile):
  payload={'bundle':{'initial_evidence_profile':profile},'manifest':[]}
  return next(t['function']['parameters']['properties'] for t in role_tools('confirmation',payload,{}) if t['function']['name']=='query_states')
 assert schema('a1-readable-1')['detail']['enum']==['summary','full']
 assert 'detail' not in schema(None)

def test_readable_confirmation_schema_cannot_use_context_state_as_event_support():
 from aiops_v4.agents.structured import result_tool
 payload={'bundle':{'initial_evidence_profile':'a1-readable-1'},'manifest':[{'vector_id':'candidate'}]}
 seen={('state','candidate'):{'vector_id':'candidate'},('state','context'):{'vector_id':'context'}}
 schema=result_tool('confirmation',payload,seen)['function']['parameters']
 refs=schema['properties']['assessments']['items']['properties']['citations']['items']['anyOf']
 for ref in refs:
  p=ref['properties']
  if 'support' in p['stance']['enum']:assert 'context' not in p['id']['enum']
 assert any('context' in r['properties']['id']['enum'] and 'counter' in r['properties']['stance']['enum'] for r in refs)

def test_readable_tools_only_offer_real_groups_and_pending_chunk_offsets():
 from aiops_v4.agents.structured import role_tools
 payload={'bundle':{'initial_evidence_profile':'a1-readable-1'},'manifest':[]}
 seen={('state','s'):{'group_id':'real-group','vector_id':'s'}}
 def named(chunks):
  return {t['function']['name']:t['function'] for t in role_tools('confirmation',payload,seen,chunks=chunks)}
 toolschema=named({})
 assert toolschema['query_model']['parameters']['properties']['group_id']['enum']==['real-group']
 assert 'read_chunk' not in toolschema
 assert 'read_chunk' not in named({'done':{'next':12,'total':12}})
 c=named({'open':{'next':12000,'total':15000}})['read_chunk']['parameters']['properties']
 assert c['handle']['enum']==['open'] and c['offset']['enum']==[12000]
def test_readable_review_accept_schema_forbids_unresolved_contradictions():
 from aiops_v4.agents.structured import result_tool
 payload={'bundle':{'initial_evidence_profile':'a1-readable-1'},'event':{'window_ids':['s']}}
 seen={('state','s'):{'vector_id':'s','source':'node'}}
 schema=result_tool('review',payload,seen)['function']['parameters']
 accept=[v for v in schema['anyOf'] if v['properties']['decision']['enum']==['accept']][0]
 assert accept['properties']['contradictions']['maxItems']==0
def test_readable_business_symptoms_do_not_offer_physical_loss_without_mechanism():
 from aiops_v4.agents.structured import result_tool
 payload={'bundle':{'initial_evidence_profile':'a1-readable-1'},'event':{'window_ids':['s']}}
 seen={('state','s'):{'vector_id':'s','source':'traffic','feature_values':{'dns_flow_error_total::rate_mean':{'raw_value':1.0}}}}
 schema=result_tool('classification',payload,seen)['function']['parameters']
 resolved=[x for x in schema['anyOf'] if x['properties']['decision']['enum']==['resolved']][0]
 pairs=resolved['properties']['category']['anyOf']
 assert not any(p['properties']['major_category']['enum']==['link'] for p in pairs)
 seen[('state','interface')]={'vector_id':'interface','source':'interface','feature_values':{'rx_drop_rate::mean':{'raw_value':2.0}}}
 schema=result_tool('classification',payload,seen)['function']['parameters']
 resolved=[x for x in schema['anyOf'] if x['properties']['decision']['enum']==['resolved']][0]
 assert any(p['properties']['major_category']['enum']==['link'] and p['properties']['sub_category']['enum']==['loss'] for p in resolved['properties']['category']['anyOf'])

def test_readable_localizer_needs_more_than_a_probe_symptom_for_client_root():
 from aiops_v4.agents.structured import result_tool,validate_result
 payload={'bundle':{'initial_evidence_profile':'a1-readable-1'},'event':{'window_ids':['s']}}
 seen={('state','s'):{'vector_id':'s','source':'traffic','entity_id':'wuhan-traffic-vm'},
       ('state','n'):{'vector_id':'n','source':'node','entity_id':'beida-service-vm-1','candidate':False}}
 schema=result_tool('localization',payload,seen)['function']['parameters']
 resolved=[r for r in schema['anyOf'] if r['properties']['decision']['enum']==['resolved']][0]
 ids=resolved['properties']['candidates']['items']['properties']['network_element_id']['enum']
 assert 'beida-service-vm-1' in ids and 'wuhan-traffic-vm' not in ids
 out={'decision':'resolved','candidates':[{'network_element_id':'wuhan-traffic-vm','confidence':.9,'reason':'only probe errors','citations':[{'kind':'state','id':'s','stance':'support','claim':'probe errors'}]}],'reason':'probe errors','missing_evidence':[]}
 with pytest.raises(ValueError,match='non-probe'):validate_result('localization',out,payload,seen)

@pytest.mark.parametrize('role,out',[('localization',{'decision':'resolved','candidates':[1]}),('classification',{'decision':'resolved','category':1})])
def test_readable_guards_keep_malformed_outputs_in_contract_validation(role,out):
 from aiops_v4.agents.structured import validate_result
 with pytest.raises(ValueError):validate_result(role,out,{'bundle':{'initial_evidence_profile':'a1-readable-1'},'event':{}},{})

def test_readable_dynamic_tool_schema_replays_complete_queries(evidence):
 from test_structured_roles import StructuredReplay,terminal
 from aiops_v4.agents.engine import run_role,recorded_delivery
 from aiops_v4.agents.contracts import load_manifest
 from aiops_v4.agents.jsonio import dumps
 db,packet=evidence;packet=dict(packet,initial_evidence_profile='a1-readable-1')
 session=tools.EvidenceSession(db,'stage2',packet);manifest=load_manifest(db,'stage2',packet['bundle_id'])
 session.prepare_candidates([w['vector_id'] for w in manifest]);visible=next(w['vector_id'] for w in manifest if ('state',w['vector_id']) in session.seen)
 group=next(iter(session.groups))
 def query(name,args,n):
  return dict(role='assistant',content=None,tool_calls=[dict(id='query'+str(n),type='function',function=dict(name=name,arguments=dumps(args)))])
 from test_evidence_first_roles import encoded
 final=encoded(dict(component_assessments={'c0':dict(decision='confirmed',confidence=.8,reason='observed fixture',support=[dict(kind='state',id=visible,claim='actual candidate')],mechanism_support=[],counter_citations=[],missing_evidence=[])}))
 messages=[query('query_states',dict(entity_id=packet['entity_id'],source='node',start=packet['start_time'],end=packet['end_time']),0),
           query('query_model',dict(group_id=group),1),terminal(final)]
 backend=StructuredReplay('fixture-model',[dict(role='confirmation',case_id='case',turn=i,message=m) for i,m in enumerate(messages)])
 run=run_role(backend,session,'confirmation','case',dict(bundle=packet,manifest=manifest))
 assert run['status']=='completed'
 assert all(t['result']['complete'] for tr in run['trace'] for t in tr['tools'] if 'result' in t)
 recorded_delivery(run,'confirmation','case')

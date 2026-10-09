import copy
import importlib
import json
import pytest
from test_agent_tools import evidence
from test_agent_diagnosis import confirmed, localization, classification
from test_agent_pipeline import replay_entries, rows
from test_agent_review_export import review
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.agents.export import prediction_for


def runner(): return importlib.import_module('aiops_v4.experiments.roles').run_diagnosis


def reply(role,case,output):
    return dict(role=role,case_id=case,turn=0,message={'role':'assistant','content':json.dumps(output)})


def joint_entries(db, packet):
    entries=replay_entries(db,packet);event,_=confirmed(db,packet);identifier=event['citations'][0]['id']
    return [entries[0],reply('joint_diagnosis',event['event_id'],dict(localization=localization(identifier),
             classification=classification(identifier))),entries[-1]]


def test_public_context_reaches_all_independent_roles_without_answer_exchange(evidence,tmp_path):
    db,packet=evidence;backend=ReplayBackend('fixture-model',replay_entries(db,packet))
    summary=runner()(db,'stage2',tmp_path/'full',backend,bundle_id=packet['bundle_id'])
    assert summary['predictions']==1 and summary['role_runs']==4 and not summary['experimental']
    payloads={r['role']:json.loads(r['messages'][1]['content']) for r in backend.requests}
    assert payloads['localization']==payloads['classification']
    assert all(p['context']['knowledge']['source'].startswith('https://challenge.aiops.cn/') for p in payloads.values())
    assert 'independent_diagnoses' not in payloads['classification']
    assert 'independent_diagnoses' in payloads['review']
    assert rows(tmp_path/'full'/'diagnoses.jsonl')[0]['accepted']


def test_joint_ablation_has_one_genuine_trace_and_strict_export_refuses_it(evidence,tmp_path):
    db,packet=evidence;backend=ReplayBackend('fixture-model',joint_entries(db,packet));out=tmp_path/'joint'
    summary=runner()(db,'stage2',out,backend,bundle_id=packet['bundle_id'],mode='joint')
    assert summary['predictions']==1 and summary['role_runs']==3 and summary['experimental']
    assert [r['role'] for r in rows(out/'roles.jsonl')]==['confirmation','joint_diagnosis','review']
    item=rows(out/'diagnoses.jsonl')[0]
    assert item['localization']['run'] is None and item['classification']['run'] is None
    assert item['joint']['run']['model']=='fixture-model'
    with pytest.raises(ValueError): prediction_for(item)
    review_payload=json.loads(backend.requests[-1]['messages'][1]['content'])
    assert review_payload['diagnostic_mode']=='joint' and 'independent_diagnoses' not in review_payload


def test_program_review_records_absent_semantic_review_without_fake_trace(evidence,tmp_path):
    db,packet=evidence;backend=ReplayBackend('fixture-model',replay_entries(db,packet)[:-1]);out=tmp_path/'program'
    summary=runner()(db,'stage2',out,backend,bundle_id=packet['bundle_id'],review_mode='program_only')
    assert summary['predictions']==1 and summary['role_runs']==3 and summary['semantic_review'] is False
    item=rows(out/'diagnoses.jsonl')[0]
    assert item['review']['run'] is None and item['review']['mode']=='program_only'
    with pytest.raises(ValueError): prediction_for(item)


@pytest.mark.parametrize('fault',['unseen','illegal_category','deferred'])
def test_joint_program_ablation_never_exports_unresolved_or_invalid_answers(evidence,tmp_path,fault):
    db,packet=evidence;entries=joint_entries(db,packet)[:-1]
    output=json.loads(entries[1]['message']['content'])
    if fault=='unseen': output['classification']['citations'][0]['id']='invented'
    if fault=='illegal_category': output['classification']['category']['major_category']='resource'
    if fault=='deferred': output['localization']=dict(decision='deferred',candidates=[],reason='Unknown causal root',missing_evidence=['causal observations'])
    entries[1]['message']['content']=json.dumps(output)
    out=tmp_path/fault;summary=runner()(db,'stage2',out,ReplayBackend('fixture-model',entries),
        bundle_id=packet['bundle_id'],mode='joint',review_mode='program_only')
    assert summary['predictions']==0 and summary['diagnoses_deferred']==1
    assert not summary['diagnosis_complete']


def test_optional_state_explanation_is_grounded_and_cannot_bypass_confirmation(evidence,tmp_path):
    db,packet=evidence;event,_=confirmed(db,packet);identifier=event['citations'][0]['id']
    output=dict(summary='Observed peer state change; not a fault label.',citations=event['citations'],limits=['Reference not known healthy'])
    entries=[reply('state_explanation',packet['bundle_id'],output)]+replay_entries(db,packet)
    backend=ReplayBackend('fixture-model',entries);out=tmp_path/'explain'
    summary=runner()(db,'stage2',out,backend,bundle_id=packet['bundle_id'],state_explanation=True,knowledge=False)
    assert summary['role_runs']==5 and summary['predictions']==1
    assert [r['role'] for r in rows(out/'roles.jsonl')][0]=='state_explanation'
    payload=json.loads(backend.requests[1]['messages'][1]['content'])
    assert payload['context']['state_explanation']['result']==output
    assert 'knowledge' not in payload['context']
    entries[0]['message']['content']=json.dumps(dict(summary='Unsupported',citations=[dict(event['citations'][0],id='fake')],limits=['missing']))
    out2=tmp_path/'bad-explanation';backend2=ReplayBackend('fixture-model',entries)
    assert runner()(db,'stage2',out2,backend2,bundle_id=packet['bundle_id'],state_explanation=True)['predictions']==1
    assert rows(out2/'roles.jsonl')[0]['error_code']=='invalid_role_contract'
    assert [r['role'] for r in backend2.requests][1]=='confirmation'

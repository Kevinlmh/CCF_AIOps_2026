"""Whole-project review regressions: real request delivery and publication guards."""
import copy
import json
import os
from pathlib import Path
import pytest
from test_agent_tools import evidence
from test_agent_pipeline import replay_entries, rows
from test_agent_diagnosis import confirmed
from test_experiment_roles import joint_entries
from test_experiment_run import fixture_config
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.agents.engine import Budget
from aiops_v4.agents.export import prediction_for
from aiops_v4.experiments.run import run_experiment, verify_run
from aiops_v4.experiments.evaluation import evaluate_run
from aiops_v4.experiments.roles import run_diagnosis, diagnose_case, experiment_prediction_for


@pytest.mark.parametrize('target', ['states/models.jsonl', 'config.json', 'states/extra.json'])
def test_previously_published_stage_cannot_change_during_agents(evidence, tmp_path, target):
    db, packet = evidence; out = tmp_path/'run'
    class MutatingReplay(ReplayBackend):
        def complete(self, *args, **kwargs):
            (out/target).write_text('{}\n')
            return super().complete(*args, **kwargs)
    config = fixture_config(tmp_path/'raw');config['agents'] = dict(bundle_id=packet['bundle_id'])
    with pytest.raises(ValueError):
        run_experiment(config, out, backend=MutatingReplay('fixture-model', replay_entries(db, packet)))
    assert not (out/'summary.json').exists() and (out/'failure.json').exists()
    with pytest.raises(ValueError):verify_run(out)


@pytest.mark.parametrize('source', ['replay', 'topology'])
@pytest.mark.parametrize('alias', [False, True])
def test_consumed_external_input_cannot_be_reused_as_truth(evidence, tmp_path, source, alias):
    db, packet = evidence; config = fixture_config(tmp_path/'raw')
    config['agents'] = dict(bundle_id=packet['bundle_id'])
    replay = tmp_path/'replay.jsonl'; replay.write_text(''.join(json.dumps(e)+'\n' for e in replay_entries(db, packet)))
    topology = tmp_path/'topology.json'; topology.write_text(json.dumps(dict(provenance='fixture', edges=[])))
    config['backend']['replay_file'] = str(replay)
    config['evidence'] = dict(topology=str(topology))
    out = tmp_path/'run'; summary = run_experiment(config, out)
    consumed = replay if source == 'replay' else topology
    truth = consumed
    if alias:
        truth = tmp_path/'truth-hardlink.jsonl'; os.link(consumed, truth)
    truth.write_text(json.dumps(dict(ground_truth_id='fixture', start_time='2026-09-17T04:12:00Z',
        end_time='2026-09-17T04:14:00Z', root_cause=dict(network_element_id='xian-br-1'),
        fault_category=dict(major_category='routing',sub_category='bgp_session_down')))+'\n')
    with pytest.raises(ValueError, match='isolated'):
        evaluate_run(out, truth, 'stage2', tmp_path/'evaluation', allow_partial=True)
    assert not (tmp_path/'evaluation').exists()
    assert {i['kind'] for i in summary['external_inputs']} == {'replay', 'topology'}
    assert all(len(i['sha256']) == 64 for i in summary['external_inputs'])


@pytest.mark.parametrize('mode', ['independent', 'joint'])
def test_every_actual_role_prompt_delivers_registered_relation_ids(evidence, tmp_path, mode):
    from aiops_v4.evidence.pipeline import build_evidence
    from aiops_v4.evidence.index import query_evidence
    db, packet = evidence
    topology = tmp_path/'topology.json'
    topology.write_text(json.dumps(dict(provenance='fixture public edge',edges=[dict(a='xian-br-1',b='xian-br-2',relation='neighbor')])))
    new = tmp_path/'related'; build_evidence(tmp_path/'states', 'stage2', new, topology=topology)
    db = new/'evidence.sqlite';packet = query_evidence(db,'stage2','bundle',entity_id='xian-br-1')[0]
    assert packet['relations']
    backend = ReplayBackend('fixture-model', replay_entries(db,packet) if mode=='independent' else joint_entries(db,packet))
    run_diagnosis(db,'stage2',tmp_path/'diagnosis',backend,bundle_id=packet['bundle_id'],mode=mode)
    expected = ['confirmation','localization','classification','review'] if mode=='independent' else ['confirmation','joint_diagnosis','review']
    assert [r['role'] for r in backend.requests] == expected
    ids = []
    for request in backend.requests:
        relations = json.loads(request['messages'][1]['content'])['bundle']['relations']
        assert all(isinstance(r.get('relation_id'),str) and len(r['relation_id'])==64 for r in relations)
        ids.append([r['relation_id'] for r in relations])
    assert all(i == ids[0] for i in ids)


@pytest.mark.parametrize('mode', ['independent','joint'])
def test_direct_diagnosis_cannot_accept_an_event_without_confirmation_support(evidence, mode):
    db, packet = evidence;event,_ = confirmed(db,packet);event['citations'] = []
    backend = ReplayBackend('fixture-model', replay_entries(db,packet) if mode=='independent' else joint_entries(db,packet))
    with pytest.raises(ValueError):
        diagnose_case(backend,db,'stage2',packet,event,Budget(),None,mode,'program_only')
    assert not backend.requests


@pytest.mark.parametrize('mode', ['independent', 'joint'])
@pytest.mark.parametrize('fault', ['no_trace','wrong_count','changed_final','invented_proof','no_confirmation','changed_confirmation'])
def test_export_rechecks_recorded_delivery_and_real_role_traces(evidence,tmp_path,mode,fault):
    db,packet=evidence
    backend=ReplayBackend('fixture-model',replay_entries(db,packet) if mode=='independent' else joint_entries(db,packet))
    out=tmp_path/'roles';run_diagnosis(db,'stage2',out,backend,bundle_id=packet['bundle_id'],mode=mode)
    item=rows(out/'diagnoses.jsonl')[0];assert item['accepted']
    exporter=prediction_for if mode=='independent' else experiment_prediction_for
    assert exporter(item)
    block=item['classification'] if mode=='independent' else item['joint']
    if fault=='no_trace':block['run']['trace']=[];block['run']['backend_calls']=0
    elif fault=='wrong_count':block['run']['backend_calls']+=1
    elif fault=='changed_final':block['run']['trace'][-1]['response']['content']='{}'
    elif fault=='invented_proof':item['localization']['evidence'][0]['data']['source']='fabricated'
    elif fault=='no_confirmation':item.pop('confirmation',None)
    elif fault=='changed_confirmation':item['event']['citations']=[]
    with pytest.raises(ValueError):exporter(item)


def test_recorded_delivery_recovers_only_complete_tool_chunks(evidence):
    from aiops_v4.agents.backend import Completion
    from aiops_v4.agents.engine import run_role, recorded_delivery
    from aiops_v4.agents.tools import EvidenceSession
    db,packet=evidence
    # Query an actual full observation through several small chunks, then finish the role.
    class ReadingBackend(ReplayBackend):
        def complete(self,messages,tools,**kwargs):
            if kwargs['turn']==0:
                function=dict(name='query_states',arguments=json.dumps(dict(entity_id='xian-br-1',source='node',limit=1)))
            else:
                reply=json.loads(messages[-1]['content'])
                if reply['complete']:
                    return Completion(dict(role='assistant',content='{"assessments":[]}'),{},self.model)
                function=dict(name='read_chunk',arguments=json.dumps(dict(handle=reply['handle'],offset=reply['next_offset'])))
            return Completion(dict(role='assistant',content=None,tool_calls=[dict(id='call-'+str(kwargs['turn']),type='function',function=function)]),{},self.model)
    session=EvidenceSession(db,'stage2',copy.deepcopy(packet),max_chunk_chars=1024)
    backend=ReadingBackend('fixture-model',[])
    run=run_role(backend,session,'confirmation',packet['bundle_id'],dict(bundle=packet),Budget(max_turns=30,max_tool_calls=30))
    assert run['status']=='completed' and run['tool_calls']>1
    payload,seen=recorded_delivery(run,'confirmation',packet['bundle_id'])
    assert seen==session.seen and payload['bundle']['batch']=='stage2'
    altered=copy.deepcopy(run);altered['trace'][0]['tools'][0]['result']['next_offset']+=1
    with pytest.raises(ValueError):recorded_delivery(altered,'confirmation',packet['bundle_id'])

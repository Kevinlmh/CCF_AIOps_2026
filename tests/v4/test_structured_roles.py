import copy
import json

import pytest
from test_agent_tools import evidence
from test_agent_confirmation import assessment, cite
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.agents.contracts import load_manifest
from aiops_v4.agents.engine import run_role, recorded_delivery
from aiops_v4.agents.tools import EvidenceSession


def test_focused_observations_are_bounded_actual_candidate_metrics(evidence):
    db, packet = evidence
    session = EvidenceSession(db, 'stage2', packet)
    manifest = load_manifest(db, 'stage2', packet['bundle_id'])
    session.prepare_candidates([w['vector_id'] for w in manifest])
    focused = session.packet['candidate_observations']
    assert 0 < len(focused) <= 16
    assert {r['vector_id'] for r in focused} <= {w['vector_id'] for w in manifest}
    assert session.packet['candidate_observation_count'] == len(manifest)
    for row in focused:
        assert session.seen[('state', row['vector_id'])]['start_time'] == row['start_time']
        assert all(k.endswith(('::mean','::rate_mean','::sum','::last')) for k in row['feature_values'])
    assert 'candidate_observations' not in packet
    assert all('drivers' not in row and 'matrix' not in row for row in focused)
    assert isinstance(session.packet['candidate_reference_by_group'],dict)


def test_structured_queries_deliver_actual_metrics_without_full_matrix(evidence):
    db, packet = evidence
    session = EvidenceSession(db, 'stage2', packet)
    session.prepare_candidates([w['vector_id'] for w in load_manifest(db, 'stage2', packet['bundle_id'])])
    reply=session.call('query_states', {'entity_id':packet['entity_id'],'source':'node','limit':1})
    assert reply['complete']
    rows=json.loads(reply['text'])
    assert rows and 'matrix' not in rows[0] and rows[0]['feature_values']


def test_candidate_observations_follow_time_even_when_event_ids_are_hash_sorted(evidence):
    db,packet=evidence;session=EvidenceSession(db,'stage2',packet)
    manifest=load_manifest(db,'stage2',packet['bundle_id'])
    session.prepare_candidates([w['vector_id'] for w in reversed(manifest)])
    rows=session.packet['candidate_observations']
    assert [r['start_time'] for r in rows]==sorted(r['start_time'] for r in rows)
    with pytest.raises(ValueError):session.prepare_candidates(['not-in-this-bundle'])


def test_local_comparison_uses_same_native_group_and_scorable_nontriggers(evidence):
    db,packet=evidence;session=EvidenceSession(db,'stage2',packet)
    manifest=load_manifest(db,'stage2',packet['bundle_id'])
    session.prepare_candidates([w['vector_id'] for w in manifest])
    comparisons=session.packet['local_comparison_observations']
    assert comparisons and len(comparisons)<=8
    groups={w['group_id'] for w in manifest}
    for row in comparisons:
        assert row['group_id'] in groups and row['candidate'] is False
        assert row['scoring_status']=='scored' and not row.get('raw_candidate',False)
        assert ('state',row['vector_id']) in session.seen


def test_default_candidate_sample_preserves_coverage_and_bounded_display(tmp_path):
    from test_evidence_pipeline import inputs
    from aiops_v4.evidence.pipeline import build_evidence
    from aiops_v4.evidence.index import query_evidence
    _, _, states = inputs(tmp_path, interfaces=12)
    directory = tmp_path/'evidence'
    build_evidence(states, 'stage2', directory)
    database = directory/'evidence.sqlite'
    packet = query_evidence(database, 'stage2', 'bundle', entity_id='xian-br-1')[0]
    session = EvidenceSession(database, 'stage2', packet)
    manifest = load_manifest(database, 'stage2', packet['bundle_id'])
    assert len(manifest)>6
    session.prepare_candidates([w['vector_id'] for w in manifest], maximum=6)
    rows = session.packet['candidate_observations']
    assert len(rows)==6 and session.packet['candidate_observations_truncated'] is True
    assert session.packet['candidate_observation_count']==len(manifest)
    assert rows[0]['vector_id']==manifest[0]['vector_id'] and rows[-1]['vector_id']==manifest[-1]['vector_id']


def test_query_budget_switches_to_terminal_schema_without_forcing_a_decision(evidence):
    from aiops_v4.agents.structured import role_tools
    db, packet = evidence
    session = EvidenceSession(db, 'stage2', packet)
    payload = dict(bundle=packet, manifest=load_manifest(db, 'stage2', packet['bundle_id']))
    tools = role_tools('confirmation', payload, session.seen, query_turns=2)
    assert [t['function']['name'] for t in tools] == ['submit_role_result']
    params = tools[0]['function']['parameters']
    assert params['properties']['assessments']['items']['properties']['decision']['enum'] == ['confirmed', 'rejected', 'deferred']


def test_confirmation_group_schema_growth_is_linear_not_quadratic():
    from aiops_v4.agents.structured import result_tool
    def schema(n):
        return result_tool('confirmation', {'manifest': [dict(vector_id='v'+str(i)) for i in range(n)]}, {})
    small, large = schema(100), schema(200)
    assert len(json.dumps(large)) < 2.8 * len(json.dumps(small))


@pytest.mark.parametrize('group',['a000','a100','other',None])
def test_linear_group_syntax_still_enforces_canonical_number_and_range(group):
    from aiops_v4.agents.structured import canonical_result
    payload={'manifest':[dict(vector_id='v'+str(i)) for i in range(100)]}
    arguments=dict(assignments={'w'+str(i):group for i in range(100)},assessments=[])
    with pytest.raises(ValueError,match='group range'):
        canonical_result('confirmation',arguments,payload)


def test_structured_state_query_limit_is_enforced_by_schema_and_runtime(evidence):
    from aiops_v4.agents.structured import role_tools
    db, packet = evidence
    session = EvidenceSession(db, 'stage2', packet)
    manifest = load_manifest(db, 'stage2', packet['bundle_id'])
    session.prepare_candidates([w['vector_id'] for w in manifest])
    tools = role_tools('confirmation', dict(bundle=session.packet, manifest=manifest), session.seen)
    states = next(t for t in tools if t['function']['name'] == 'query_states')
    assert states['function']['parameters']['properties']['limit']['maximum'] == 1
    assert states['function']['strict'] is True
    with pytest.raises(ValueError, match='compact state page'):
        session.call('query_states', {'entity_id': packet['entity_id'], 'source': 'node', 'limit': 100})
    # Legacy read-only API retains its larger pagination capacity.
    legacy = EvidenceSession(db, 'stage2', packet)
    assert legacy.call('query_states', {'limit': 100})


def test_deferred_diagnostic_schema_requires_abstention(evidence):
    from aiops_v4.agents.structured import result_tool
    db,packet=evidence;session=EvidenceSession(db,'stage2',packet)
    left=result_tool('localization',dict(bundle=packet),session.seen)['function']['parameters']
    choices=left['anyOf']
    deferred=next(s for s in choices if s['properties']['decision']['enum']==['deferred'])
    assert deferred['properties']['candidates']['maxItems']==0
    resolved=next(s for s in choices if s['properties']['decision']['enum']==['resolved'])
    assert resolved['properties']['candidates']['minItems']==1
    right=result_tool('classification',dict(bundle=packet),session.seen)['function']['parameters']
    deferred=next(s for s in right['anyOf'] if s['properties']['decision']['enum']==['deferred'])
    assert deferred['properties']['category']=={'type':'null'}


class StructuredReplay(ReplayBackend):
    structured_roles = True

    def metadata(self):
        return dict(super().metadata(), structured_roles=True)


def terminal(output):
    return dict(role='assistant', content=None, tool_calls=[dict(id='final', type='function',
        function=dict(name='submit_role_result', arguments=json.dumps(output)))])


def fixture_role(evidence, outputs):
    db, packet = evidence
    session = EvidenceSession(db, 'stage2', packet)
    manifest = load_manifest(db, 'stage2', packet['bundle_id'])
    def wire(output):
        assignments={};items=[]
        for i,item in enumerate(output['assessments']):
            group='a'+str(i);ids=item['window_ids']
            if ids==['*']:ids=[w['vector_id'] for w in manifest]
            assignments.update({'w'+str(j):group for j,w in enumerate(manifest) if w['vector_id'] in ids})
            items.append(dict({k:v for k,v in item.items() if k!='window_ids'},group_id=group))
        return dict(assignments=assignments,assessments=items)
    api = StructuredReplay('fixture-model', [dict(role='confirmation', case_id='case', turn=i,
        message=terminal(wire(o))) for i, o in enumerate(outputs(session, manifest))])
    return api, run_role(api, session, 'confirmation', 'case', dict(bundle=packet, manifest=manifest))


def good_output(session, manifest):
    visible = next(w['vector_id'] for w in manifest if ('state', w['vector_id']) in session.seen)
    return {'assessments': [assessment(['*'], [cite(visible)])]}


def test_terminal_call_is_actual_model_output_and_replays_with_exact_schema(evidence):
    from aiops_v4.agents.structured import canonical_result
    api, run = fixture_role(evidence, lambda s, m: [good_output(s, m)])
    assert run['status'] == 'completed'
    arguments=json.loads(run['trace'][0]['response']['tool_calls'][0]['function']['arguments'])
    assert run['output'] == canonical_result('confirmation',arguments,json.loads(run['initial_messages'][1]['content']))
    assert run['run_schema_version'] == 3
    payload, seen = recorded_delivery(run, 'confirmation', 'case')
    assert payload['manifest'] and seen
    final = next(t for t in api.requests[0]['tools'] if t['function']['name'] == 'submit_role_result')
    assert final['function']['strict'] is True
    bad = copy.deepcopy(run)
    bad['initial_tools'][-1]['function']['parameters'] = {'type': 'object'}
    with pytest.raises(ValueError): recorded_delivery(bad, 'confirmation', 'case')


def test_incomplete_assignment_gets_one_validation_feedback_not_auto_filled(evidence):
    def outputs(s, m):
        good = good_output(s, m)
        bad = copy.deepcopy(good)
        bad['assessments'][0]['window_ids'] = [bad['assessments'][0]['citations'][0]['id']]
        return [bad, good]
    api, run = fixture_role(evidence, outputs)
    assert run['status'] == 'completed' and run['backend_calls'] == 2
    assert run['trace'][0]['repair'] == 'invalid_role_contract'
    assert 'all candidate windows must be assigned exactly once' in api.requests[1]['messages'][-1]['content']
    recorded_delivery(run, 'confirmation', 'case')
    bad = copy.deepcopy(run)
    bad['trace'][0]['tools'][0]['feedback']['error'] = 'accepted'
    with pytest.raises(ValueError): recorded_delivery(bad, 'confirmation', 'case')


def test_repeated_unseen_citation_is_bounded_and_never_exported(evidence):
    def outputs(s, m):
        bad = good_output(s, m)
        bad['assessments'][0]['citations'][0]['id'] = 'invented'
        return [bad, bad]
    _, run = fixture_role(evidence, outputs)
    assert run['status'] == 'deferred' and run['output'] is None
    assert run['error_code'] == 'invalid_role_contract' and run['backend_calls'] == 2


def test_recorded_resource_comparison_cannot_replace_delivered_metrics(evidence):
    from aiops_v4.agents.jsonio import dumps
    _, run = fixture_role(evidence, lambda s, m: [good_output(s, m)])
    bad = copy.deepcopy(run)
    payload = json.loads(bad['initial_messages'][1]['content'])
    payload['bundle']['resource_change_summary'] = {'fabricated': {}}
    bad['initial_messages'][1]['content'] = dumps(payload)
    with pytest.raises(ValueError, match='resource comparison differs'):
        recorded_delivery(bad, 'confirmation', 'case')


@pytest.mark.parametrize('queried_role',[None,'localization','classification'])
def test_four_structured_roles_export_only_after_real_protocol_validation(evidence,tmp_path,queried_role):
    from test_agent_pipeline import replay_entries,rows
    from aiops_v4.agents.pipeline import diagnose
    db,packet=evidence;manifest=load_manifest(db,'stage2',packet['bundle_id'])
    entries=replay_entries(db,packet)
    node_id=next(s['vector_id'] for s in packet['support'] if s['source']=='node')
    def use_node_citation(value):
        if isinstance(value,list):return [use_node_citation(v) for v in value]
        if isinstance(value,dict):
            if value.get('kind')=='state' and 'id' in value:return dict(value,id=node_id)
            return {k:use_node_citation(v) for k,v in value.items()}
        return value
    for entry in entries:
        output=use_node_citation(json.loads(entry['message']['content']))
        if entry['role']=='classification':output['category']=dict(major_category='resource',sub_category='cpu_pressure')
        if entry['role']=='confirmation':
            item=output['assessments'][0]
            assert set(item['window_ids'])=={w['vector_id'] for w in manifest}
            output=dict(assignments={'w'+str(i):'a0' for i in range(len(manifest))},
                assessments=[dict({k:v for k,v in item.items() if k!='window_ids'},group_id='a0')])
        entry['message']=terminal(output)
    if queried_role:
        target=next(e for e in entries if e['role']==queried_role)
        output=json.loads(target['message']['tool_calls'][0]['function']['arguments'])
        citation=(output['candidates'][0]['citations'][0] if queried_role=='localization' else output['citations'][0])
        window=next(w for w in manifest if w['vector_id']==citation['id'])
        target['turn']=1
        entries.append(dict(role=queried_role,case_id=target['case_id'],turn=0,message=dict(role='assistant',content=None,
            tool_calls=[dict(id='query',type='function',function=dict(name='query_states',arguments=json.dumps(dict(
                entity_id=packet['entity_id'],group_id=window['group_id'],source=window['source'],
                start=window['start_time'],end=window['end_time'],limit=1))))])))
    summary=diagnose(db,'stage2',tmp_path/'run',StructuredReplay('fixture-model',entries),bundle_id=packet['bundle_id'])
    assert summary['predictions']==1 and summary['role_failures']==0
    assert rows(tmp_path/'run'/'diagnoses.jsonl')[0]['accepted'] is True
    for run in rows(tmp_path/'run'/'roles.jsonl'):
        if run['role'] in ('localization','classification','review'):
            event=json.loads(run['initial_messages'][1]['content'])['event']
            assert 'reason' not in event and 'missing_evidence' not in event and 'confidence' not in event
            assert event['window_ids'] and event['event_id']

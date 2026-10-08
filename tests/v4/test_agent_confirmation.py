import copy
import importlib
import json

import pytest
from test_agent_tools import evidence
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.agents.tools import EvidenceSession


def contracts(): return importlib.import_module('aiops_v4.agents.contracts')
def engine(): return importlib.import_module('aiops_v4.agents.engine')


def cite(identifier, kind='state', stance='support'):
    return dict(kind=kind, id=identifier, stance=stance, claim='Observed candidate supports this assessment; fixture only.')


def assessment(ids, citations, decision='confirmed'):
    return dict(decision=decision, window_ids=ids, confidence=0.7, reason='Fixture assessment, not model accuracy.',
                citations=citations, missing_evidence=['physical onset unknown'] if decision == 'deferred' else [])


def test_confirmation_splits_native_views_and_preserves_every_window(evidence):
    db, packet = evidence
    manifest = contracts().load_manifest(db, 'stage2', packet['bundle_id'])
    session = EvidenceSession(db, 'stage2', packet)
    assert len(manifest) >= 3 and all(w['event_id'] for w in manifest)
    groups = {}
    for w in manifest: groups.setdefault(w['source'], []).append(w['vector_id'])
    output = {'assessments': [assessment(ids, [cite(next(i for i in ids if ('state', i) in session.seen))]) for ids in groups.values()]}
    result = contracts().validate_confirmation(output, manifest, session.seen)
    assert len(result) == 3 and len({r['event_id'] for r in result}) == 3
    assert sorted(i for r in result for i in r['window_ids']) == sorted(w['vector_id'] for w in manifest)
    for r in result:
        windows = [w for w in manifest if w['vector_id'] in r['window_ids']]
        assert r['start_time'] == min(w['start_time'] for w in windows)
        assert r['end_time'] == max(w['end_time'] for w in windows)
        assert r['boundary_basis'] == 'observed_candidate_windows; physical_onset_unknown'
    assert contracts().validate_confirmation(output, manifest, session.seen) == result


@pytest.mark.parametrize('fault', ['unassigned', 'duplicate', 'invented', 'unseen', 'confidence', 'extra', 'no_support', 'empty_missing'])
def test_confirmation_rejects_fabricated_or_incomplete_evidence(evidence, fault):
    db, packet = evidence
    manifest = contracts().load_manifest(db, 'stage2', packet['bundle_id'])
    session = EvidenceSession(db, 'stage2', packet)
    ids = [w['vector_id'] for w in manifest]
    visible = next(w for w in ids if ('state', w) in session.seen)
    row = assessment(ids, [cite(visible)])
    if fault == 'unassigned': row['window_ids'].pop()
    if fault == 'duplicate': row['window_ids'].append(ids[0])
    if fault == 'invented': row['window_ids'][0] = 'invented'
    if fault == 'unseen': row['citations'] = [cite('invented')]
    if fault == 'confidence': row['confidence'] = float('nan')
    if fault == 'extra': row['start_time'] = '2020-01-01T00:00:00Z'
    if fault == 'no_support': row['citations'][0]['stance'] = 'context'
    if fault == 'empty_missing': row['decision'] = 'deferred'
    with pytest.raises(ValueError): contracts().validate_confirmation({'assessments': [row]}, manifest, session.seen)


def test_noncontiguous_windows_must_be_split():
    manifest = [dict(vector_id='a', event_id='native-a', source='node', start_time='2026-09-17T04:00:00Z', end_time='2026-09-17T04:01:00Z'),
                dict(vector_id='b', event_id='native-b', source='node', start_time='2026-09-17T05:00:00Z', end_time='2026-09-17T05:01:00Z')]
    seen = {('state', w['vector_id']): w for w in manifest}
    with pytest.raises(ValueError, match='continuous'):
        contracts().validate_confirmation({'assessments': [assessment(['a', 'b'], [cite('a')])]}, manifest, seen)


def test_manifest_budget_is_execution_limit_not_silent_drop(evidence):
    db, packet = evidence
    with pytest.raises(ValueError, match='manifest_budget'):
        contracts().load_manifest(db, 'stage2', packet['bundle_id'], max_windows=1)


def test_role_native_tool_round_trip_has_fresh_messages_and_usage(evidence):
    db, packet = evidence; session = EvidenceSession(db, 'stage2', packet)
    api = ReplayBackend('fixture-model', [
        dict(role='confirmation', case_id='case', turn=0, usage={'total_tokens': 8}, message={'role': 'assistant', 'content': None,
             'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'query_members', 'arguments': '{"limit":1}'}}]}),
        dict(role='confirmation', case_id='case', turn=1, usage={'total_tokens': 2}, message={'role': 'assistant', 'content': '{"assessments":[]}'})])
    run = engine().run_role(api, session, 'confirmation', 'case', {'bundle': packet})
    assert run['status'] == 'completed' and run['usage']['total_tokens'] == 10
    assert run['output'] == {'assessments': []} and len(run['trace']) == 2
    assert api.requests[0]['messages'][0]['role'] == 'system'
    assert '不可信' in api.requests[0]['messages'][0]['content']
    tool_message = api.requests[1]['messages'][-1]
    assert tool_message['role'] == 'tool' and tool_message['tool_call_id'] == 'call-1'
    assert json.loads(tool_message['content'])['complete'] is True
    assert run['trace'][0]['request_sha256'] and run['model'] == 'fixture-model'


@pytest.mark.parametrize('fault', ['turn_budget', 'tool_budget', 'prompt_budget', 'response_budget', 'invalid_json', 'unknown_tool', 'refusal'])
def test_role_failure_is_explicit_deferred_not_invented_result(evidence, fault):
    db, packet = evidence
    msg = {'role': 'assistant', 'content': '{"assessments":[]}'}
    values = {}
    if fault in {'turn_budget', 'tool_budget', 'unknown_tool'}:
        msg = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'a', 'type': 'function', 'function': {'name': 'delete' if fault == 'unknown_tool' else 'query_members', 'arguments': '{}'}}]}
        values = {'max_turns': 1} if fault == 'turn_budget' else {'max_tool_calls': 0} if fault == 'tool_budget' else {}
    if fault == 'prompt_budget': values['max_prompt_chars'] = 100
    if fault == 'response_budget': values['max_response_chars'] = 10
    if fault == 'invalid_json': msg['content'] = 'wrong'
    if fault == 'refusal': msg['refusal'] = 'private refusal body'
    api = ReplayBackend('fixture-model', [dict(role='confirmation', case_id='case', turn=0, message=msg)])
    run = engine().run_role(api, EvidenceSession(db, 'stage2', packet), 'confirmation', 'case', {'bundle': packet}, engine().Budget(**values))
    assert run['status'] == 'deferred' and run['output'] is None and run['error_code']
    assert 'private refusal body' not in json.dumps(run)
    if fault == 'prompt_budget': assert not api.requests

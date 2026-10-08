import copy
import importlib
import json

import pytest
from test_agent_tools import evidence
from test_agent_confirmation import cite
from test_agent_diagnosis import confirmed, entries
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.agents.diagnosis import diagnose_event
from aiops_challenge_2026.schema import validate_prediction


def diagnosis(): return importlib.import_module('aiops_v4.agents.diagnosis')
def exporter(): return importlib.import_module('aiops_v4.agents.export')


def review(identifier, decision='accept'):
    return dict(decision=decision, reason='Consistent fixture observations; simulated only.', citations=[cite(identifier)],
                missing_evidence=[] if decision == 'accept' else ['causal ordering unclear'],
                counterevidence_assessment='Counter observations lack current triggers; not known healthy. Missing logs limit the conclusion.',
                contradictions=[])


def setup(evidence):
    db, packet = evidence; event, _ = confirmed(db, packet)
    identifier = event['citations'][0]['id']
    all_entries = entries(event, identifier) + [dict(role='review', case_id=event['event_id'], turn=0,
                              message={'role': 'assistant', 'content': json.dumps(review(identifier))})]
    api = ReplayBackend('fixture-model', all_entries)
    return api, db, packet, diagnose_event(api, db, 'stage2', packet, event)


def test_review_accepts_complete_evidence_and_export_is_official(evidence):
    api, db, packet, item = setup(evidence)
    result = diagnosis().review_event(api, db, 'stage2', packet, item)
    assert result['accepted'] is True and result['status'] == 'accepted'
    assert [r['role'] for r in api.requests] == ['localization', 'classification', 'review']
    payload = json.loads(api.requests[-1]['messages'][1]['content'])
    assert payload['independent_diagnoses']['localization']['result']['candidates']
    assert payload['independent_diagnoses']['classification']['result']['category']
    record = exporter().prediction_for(result)
    assert set(record) == {'prediction_id', 'start_time', 'end_time', 'root_cause_top5', 'fault_category'}
    assert record['root_cause_top5'] == [{'rank': 1, 'network_element_id': 'xian-br-1'}]
    assert record['start_time'] == result['event']['start_time']
    validate_prediction(record)
    assert exporter().prediction_for(result) == record
    assert list(exporter().export_predictions([result])) == [record]
    with pytest.raises(ValueError, match='duplicate'):
        list(exporter().export_predictions([result, result]))


@pytest.mark.parametrize('fault', ['failed_role', 'abstain', 'illegal_device', 'changed_interval', 'changed_window', 'wrong_model', 'different_case'])
def test_review_cannot_bypass_program_guards(evidence, fault):
    api, db, packet, item = setup(evidence)
    if fault == 'failed_role': item['classification']['run']['status'] = 'deferred'
    if fault == 'abstain': item['localization']['result']['decision'] = 'deferred'
    if fault == 'illegal_device': item['localization']['result']['candidates'][0]['network_element_id'] = 'made-up'
    if fault == 'changed_interval': item['event']['end_time'] = '2026-09-18T04:15:00Z'
    if fault == 'changed_window': item['event']['window_ids'][0] = 'invented'
    if fault == 'wrong_model': item['classification']['run']['model'] = 'different-model'
    if fault == 'different_case': item['classification']['run']['case_id'] = 'another-event'
    result = diagnosis().review_event(api, db, 'stage2', packet, item)
    assert not result['accepted'] and result['status'] == 'deferred'
    assert len(api.requests) == 2 and exporter().prediction_for(result) is None
    forged = copy.deepcopy(result); forged['accepted'] = True
    with pytest.raises(ValueError): exporter().prediction_for(forged)


@pytest.mark.parametrize('fault', ['contradictions', 'no_counter_assessment', 'new_interval', 'unseen', 'reject', 'defer'])
def test_invalid_or_negative_review_never_exports(evidence, fault):
    api, db, packet, item = setup(evidence)
    identifier = item['event']['citations'][0]['id']; output = review(identifier)
    if fault == 'contradictions': output['contradictions'] = ['Peer observation conflicts with root hypothesis.']
    if fault == 'no_counter_assessment': output['counterevidence_assessment'] = ''
    if fault == 'new_interval': output['start_time'] = '2020-01-01T00:00:00Z'
    if fault == 'unseen': output['citations'][0]['id'] = 'invented'
    if fault in {'reject', 'defer'}: output = review(identifier, fault)
    api.entries[('review', item['event']['event_id'], 0)]['message']['content'] = json.dumps(output)
    result = diagnosis().review_event(api, db, 'stage2', packet, item)
    assert not result['accepted'] and exporter().prediction_for(result) is None
    assert list(exporter().export_predictions([result])) == []


def test_export_top5_truncates_real_ranked_candidates_without_padding(evidence):
    api, db, packet, item = setup(evidence)
    result = diagnosis().review_event(api, db, 'stage2', packet, item)
    # Export behavior fixture: each additional device has an explicit observed proof and role output.
    from aiops_challenge_2026.schema import VALID_NETWORK_ELEMENTS
    cause = result['localization']['result']['candidates'][0]
    for i, node in enumerate(sorted(VALID_NETWORK_ELEMENTS - {'xian-br-1'})[:5]):
        identifier = f'export-fixture-{i}'
        result['localization']['result']['candidates'].append(dict(network_element_id=node, confidence=0.4,
            reason='Explicit export fixture observation.', citations=[cite(identifier)]))
        result['localization']['evidence'].append(dict(kind='state', id=identifier,
            data=dict(vector_id=identifier, batch='stage2', entity_id=node)))
    result['localization']['run']['output'] = copy.deepcopy(result['localization']['result'])
    record = exporter().prediction_for(result)
    assert len(record['root_cause_top5']) == 5 and record['root_cause_top5'][0]['network_element_id'] == 'xian-br-1'
    assert [r['rank'] for r in record['root_cause_top5']] == [1, 2, 3, 4, 5]
    validate_prediction(record)

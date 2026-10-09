import copy
import importlib
import json

import pytest
from test_agent_tools import evidence
from test_agent_confirmation import assessment, cite
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.agents.contracts import load_manifest, validate_confirmation
from aiops_v4.agents.tools import EvidenceSession


def contracts(): return importlib.import_module('aiops_v4.agents.contracts')
def diagnosis(): return importlib.import_module('aiops_v4.agents.diagnosis')


def confirmed(db, packet):
    manifest = load_manifest(db, 'stage2', packet['bundle_id'])
    session = EvidenceSession(db, 'stage2', packet)
    ids = [w['vector_id'] for w in manifest]
    visible = next(s['vector_id'] for s in packet['support'] if s['source'] == 'routing')
    return validate_confirmation({'assessments': [assessment(ids, [cite(visible)])]}, manifest, session.seen)[0], session


def localization(identifier):
    return dict(decision='resolved', candidates=[dict(network_element_id='xian-br-1', confidence=0.8,
                reason='The fixture peer observation belongs to this device.', citations=[cite(identifier)])],
                reason='Independent localization fixture.', missing_evidence=[])


def classification(identifier):
    return dict(decision='resolved', category=dict(major_category='routing', sub_category='bgp_session_down'),
                confidence=0.8, reason='The fixture peer up observation is zero.', citations=[cite(identifier)], missing_evidence=[])


def entries(event, identifier):
    return [dict(role=role, case_id=event['event_id'], turn=0, message={'role': 'assistant', 'content': json.dumps(output)})
            for role, output in [('localization', localization(identifier)), ('classification', classification(identifier))]]


def test_same_model_two_independent_conversations(evidence):
    db, packet = evidence; event, session = confirmed(db, packet)
    identifier = event['citations'][0]['id']
    api = ReplayBackend('fixture-model', entries(event, identifier))
    result = diagnosis().diagnose_event(api, db, 'stage2', packet, event)
    assert result['status'] == 'awaiting_review' and result['accepted'] is False
    assert result['localization']['result']['candidates'][0]['network_element_id'] == 'xian-br-1'
    assert result['classification']['result']['category']['sub_category'] == 'bgp_session_down'
    assert [r['role'] for r in api.requests] == ['localization', 'classification']
    left, right = [json.loads(r['messages'][1]['content']) for r in api.requests]
    assert left == right and 'localization' not in right and 'classification' not in left
    assert all(len(r['messages']) == 2 for r in api.requests)
    assert all(result[r]['run']['model'] == 'fixture-model' for r in ['localization', 'classification'])
    assert result['localization']['evidence'][0]['id'] == identifier
    assert result['classification']['evidence'][0]['data']['source'] == 'routing'


@pytest.mark.parametrize('fault', ['illegal_device', 'auxiliary', 'duplicate', 'wrong_device_evidence', 'unseen', 'no_support', 'confidence'])
def test_rca_requires_legal_unique_devices_with_their_own_observations(evidence, fault):
    db, packet = evidence; event, session = confirmed(db, packet)
    output = localization(event['citations'][0]['id']); cause = output['candidates'][0]
    if fault == 'illegal_device': cause['network_element_id'] = 'fictional-cr-1'
    if fault == 'auxiliary': cause['network_element_id'] = 'xian-auxiliary-host'
    if fault == 'duplicate': output['candidates'].append(copy.deepcopy(cause))
    if fault == 'wrong_device_evidence': cause['network_element_id'] = 'xian-br-2'
    if fault == 'unseen': cause['citations'][0]['id'] = 'foreign-batch-observation'
    if fault == 'no_support': cause['citations'][0]['stance'] = 'context'
    if fault == 'confidence': cause['confidence'] = True
    with pytest.raises(ValueError): contracts().validate_localization(output, session.seen)


@pytest.mark.parametrize('fault', ['illegal_pair', 'extra', 'unseen', 'other_window', 'no_support'])
def test_classifier_uses_official_pairs_and_selected_event_support(evidence, fault):
    db, packet = evidence; event, session = confirmed(db, packet)
    output = classification(event['citations'][0]['id'])
    if fault == 'illegal_pair': output['category']['major_category'] = 'resource'
    if fault == 'extra': output['category']['hint'] = 'v3'
    if fault == 'unseen': output['citations'][0]['id'] = 'invented'
    if fault == 'other_window':
        session.seen[('state', 'outside')] = {'entity_id': 'xian-br-1'}
        output['citations'][0]['id'] = 'outside'
    if fault == 'no_support': output['citations'][0]['stance'] = 'counter'
    with pytest.raises(ValueError): contracts().validate_classification(output, event, session.seen)


def test_independent_abstentions_and_invalid_output_are_preserved(evidence):
    db, packet = evidence; event, _ = confirmed(db, packet)
    identifier = event['citations'][0]['id']
    left = dict(decision='deferred', candidates=[], reason='Cannot distinguish causal root.', missing_evidence=['raw routing log absent'])
    right = classification(identifier); right['category']['major_category'] = 'fake'
    api = ReplayBackend('fixture-model', [dict(role=role, case_id=event['event_id'], turn=0, message={'role': 'assistant', 'content': json.dumps(output)})
            for role, output in [('localization', left), ('classification', right)]])
    result = diagnosis().diagnose_event(api, db, 'stage2', packet, event)
    assert result['status'] == 'deferred' and result['localization']['result']['decision'] == 'deferred'
    assert result['classification']['result'] is None
    assert result['classification']['run']['error_code'] == 'invalid_role_contract'
    assert result['classification']['run']['trace'][0]['response']


@pytest.mark.parametrize('role', ['confirmation', 'localization', 'classification'])
def test_oversized_integer_confidence_is_a_controlled_contract_failure(evidence, role):
    db, packet = evidence; event, session = confirmed(db, packet)
    identifier = event['citations'][0]['id']
    if role == 'confirmation':
        item = assessment(event['window_ids'], [cite(identifier)]); item['confidence'] = 10 ** 400
        validate = lambda: contracts().validate_confirmation({'assessments': [item]}, load_manifest(db, 'stage2', packet['bundle_id']), session.seen)
    elif role == 'localization':
        item = localization(identifier); item['candidates'][0]['confidence'] = 10 ** 400
        validate = lambda: contracts().validate_localization(item, session.seen)
    else:
        item = classification(identifier); item['confidence'] = 10 ** 400
        validate = lambda: contracts().validate_classification(item, event, session.seen)
    with pytest.raises(ValueError): validate()


def confirmation_record(db,packet,event):
    from aiops_v4.agents.engine import run_role
    from aiops_v4.agents.diagnosis import evidence_for
    session=EvidenceSession(db,'stage2',packet)
    output={'assessments':[assessment(event['window_ids'],event['citations'])]}
    backend=ReplayBackend('fixture-model',[dict(role='confirmation',case_id=packet['bundle_id'],turn=0,
        message={'role':'assistant','content':json.dumps(output)})])
    run=run_role(backend,session,'confirmation',packet['bundle_id'],dict(bundle=packet,
        manifest=load_manifest(db,'stage2',packet['bundle_id'])))
    return dict(run=run,evidence=evidence_for(event,session.seen,event))

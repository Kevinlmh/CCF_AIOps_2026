import importlib
import json

import pytest
from test_agent_tools import evidence
from test_agent_confirmation import cite
from test_agent_diagnosis import confirmation_record, confirmed, entries
from test_agent_review_export import review, setup
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.agents.diagnosis import diagnose_event, review_event
from aiops_v4.agents.contracts import load_manifest
from aiops_challenge_2026.schema import validate_prediction


def exporter():
    return importlib.import_module('aiops_v4.agents.export')


def _category(**kwargs):
    return dict(major_category=kwargs['major'], sub_category=kwargs['sub'])


def deferred_localization(identifier):
    return dict(decision='deferred', candidates=[], reason='Cannot separate the observed peers.',
                missing_evidence=['no exclusive device evidence'])


def test_review_deferred_is_exported_only_with_the_abstention_policy(evidence):
    api, db, packet, item = setup(evidence)
    identifier = item['event']['citations'][0]['id']
    api.entries[('review', item['event']['event_id'], 0)]['message']['content'] = json.dumps(review(identifier, 'defer'))
    result = review_event(api, db, 'stage2', packet, item)
    assert result['status'] == 'deferred' and not result['accepted']
    assert exporter().prediction_for(result) is None
    record = exporter().prediction_for(result, export_abstentions=True)
    assert record is not None and record['prediction_id'] == result['event']['event_id']
    validate_prediction(record)


def test_review_rejected_is_never_exported_even_with_the_policy(evidence):
    api, db, packet, item = setup(evidence)
    identifier = item['event']['citations'][0]['id']
    api.entries[('review', item['event']['event_id'], 0)]['message']['content'] = json.dumps(review(identifier, 'reject'))
    result = review_event(api, db, 'stage2', packet, item)
    assert result['status'] == 'rejected'
    assert exporter().prediction_for(result, export_abstentions=True) is None


def test_deferred_localization_falls_back_to_the_delivered_device(evidence):
    api, db, packet, item = setup(evidence)
    item = review_event(api, db, 'stage2', packet, item)
    item['localization']['result'] = deferred_localization(item['event']['citations'][0]['id'])
    item['accepted'] = False
    item['status'] = 'deferred'
    record = exporter().prediction_for(item, export_abstentions=True)
    assert record is not None
    assert record['root_cause_top5'][0]['network_element_id'] == 'xian-br-1'
    assert record['fault_category'] == dict(major_category='resource', sub_category='cpu_pressure')


def test_unsupported_llm_category_is_corrected_to_the_delivered_mechanism(evidence):
    db, packet = evidence
    event, _ = confirmed(db, packet)
    identifier = event['citations'][0]['id']
    wrong = dict(decision='resolved', category=dict(major_category='service', sub_category='web_5xx'),
                 confidence=0.6, reason='The reviewer fixture claim is not backed by this event.',
                 citations=[cite(identifier)], missing_evidence=[])
    stream = entries(event, identifier)
    stream[1]['message']['content'] = json.dumps(wrong)
    stream.append(dict(role='review', case_id=event['event_id'], turn=0,
                       message={'role': 'assistant', 'content': json.dumps(review(identifier))}))
    api = ReplayBackend('fixture-model', stream)
    item = diagnose_event(api, db, 'stage2', packet, event, confirmation=confirmation_record(db, packet, event))
    result = review_event(api, db, 'stage2', packet, item)
    assert result['accepted'] is True
    record = exporter().prediction_for(result)
    assert record['fault_category'] == dict(major_category='resource', sub_category='cpu_pressure')
    assert result['category_adjustment']['original'] == ['service', 'web_5xx']
    assert result['effective_category'] == dict(major_category='resource', sub_category='cpu_pressure')

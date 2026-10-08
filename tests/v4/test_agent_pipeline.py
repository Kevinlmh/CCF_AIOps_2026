import importlib
import json
import subprocess
import sys

import pytest
from test_agent_tools import evidence
from test_agent_backend import server
from test_agent_confirmation import assessment, cite
from test_agent_diagnosis import confirmed, entries
from test_agent_review_export import review
from test_evidence_pipeline import inputs, sha
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.agents.engine import Budget
from aiops_v4.evidence.pipeline import build_evidence
from aiops_challenge_2026.schema import validate_prediction


def pipeline(): return importlib.import_module('aiops_v4.agents.pipeline')


def replay_entries(db, packet):
    event, _ = confirmed(db, packet)
    identifier = event['citations'][0]['id']
    confirmation = {'assessments': [assessment(event['window_ids'], [cite(identifier)])]}
    return [dict(role='confirmation', case_id=packet['bundle_id'], turn=0, usage={'total_tokens': 11},
                 message={'role': 'assistant', 'content': json.dumps(confirmation)})] + entries(event, identifier) + [
            dict(role='review', case_id=event['event_id'], turn=0, message={'role': 'assistant', 'content': json.dumps(review(identifier))})]


def rows(path): return [json.loads(line) for line in path.read_text().splitlines()]


def test_real_raw_to_predictions_replay_is_auditable_and_readonly(evidence, tmp_path):
    db, packet = evidence; before = sha(db)
    api = ReplayBackend('fixture-model', replay_entries(db, packet))
    out = tmp_path / 'diagnosis'
    summary = pipeline().diagnose(db, 'stage2', out, api, bundle_id=packet['bundle_id'])
    assert summary['simulated'] is True and summary['predictions'] == 1
    assert summary['bundles_selected'] == 1 and summary['bundles_available'] == 2
    assert summary['selection_complete'] is False and summary['unselected_bundles'] == 1
    assert summary['input_evidence_sha256'] == before and sha(db) == before
    assert summary['role_runs'] == 4 and summary['usage']['total_tokens'] == 11
    predictions = rows(out / 'predictions.jsonl'); assert len(predictions) == 1
    validate_prediction(predictions[0])
    roles = rows(out / 'roles.jsonl'); assert [r['role'] for r in roles] == ['confirmation', 'localization', 'classification', 'review']
    assert all(r['prompt_version'] and r['trace'][0]['request_sha256'] for r in roles)
    assert rows(out / 'diagnoses.jsonl')[0]['accepted'] is True
    assert rows(out / 'bundles.jsonl')[0]['windows_assigned'] >= 3
    assert json.loads((out / 'summary.json').read_text()) == summary
    assert 'accuracy' not in summary


def test_role_failures_and_manifest_budget_are_explicit(evidence, tmp_path):
    db, packet = evidence
    out = tmp_path / 'missing-replay'
    summary = pipeline().diagnose(db, 'stage2', out, ReplayBackend('fixture-model', []), limit_bundles=1)
    assert summary['predictions'] == 0 and summary['bundles_deferred'] == 1
    assert rows(out / 'bundles.jsonl')[0]['error_code'] == 'replay_missing'
    assert rows(out / 'roles.jsonl')[0]['status'] == 'deferred'
    limited = tmp_path / 'budget'
    api = ReplayBackend('fixture-model', replay_entries(db, packet))
    summary = pipeline().diagnose(db, 'stage2', limited, api, bundle_id=packet['bundle_id'], budget=Budget(max_manifest_windows=1))
    assert summary['bundles_deferred'] == 1 and not api.requests
    assert rows(limited / 'bundles.jsonl')[0]['error_code'] == 'manifest_budget'
    assert summary['diagnosis_complete'] is False


def test_nonconfirmed_assessments_are_retained_without_extra_roles(evidence, tmp_path):
    db, packet = evidence; event, _ = confirmed(db, packet)
    output = {'assessments': [assessment(event['window_ids'], [], decision='deferred')]}
    api = ReplayBackend('fixture-model', [dict(role='confirmation', case_id=packet['bundle_id'], turn=0,
                        message={'role': 'assistant', 'content': json.dumps(output)})])
    out = tmp_path / 'deferred'
    summary = pipeline().diagnose(db, 'stage2', out, api, bundle_id=packet['bundle_id'])
    assert summary['predictions'] == 0 and summary['role_runs'] == 1
    assert rows(out / 'diagnoses.jsonl')[0]['event']['decision'] == 'deferred'
    assert summary['assessments_deferred'] == 1 and not summary['diagnosis_complete']


def test_empty_candidate_queue_is_valid_complete_run(tmp_path):
    _, _, states = inputs(tmp_path, interfaces=0, spike=False)
    evidence_dir = tmp_path / 'evidence'; build_evidence(states, 'stage2', evidence_dir)
    out = tmp_path / 'empty'
    summary = pipeline().diagnose(evidence_dir / 'evidence.sqlite', 'stage2', out, ReplayBackend('fixture-model', []))
    assert summary['predictions'] == summary['bundles_selected'] == summary['role_runs'] == 0
    assert summary['selection_complete'] and summary['diagnosis_complete']
    assert (out / 'predictions.jsonl').read_text() == ''


def test_output_protection_and_scope_fail_before_publication(evidence, tmp_path):
    db, packet = evidence; api = ReplayBackend('fixture-model', [])
    existing = tmp_path / 'existing'; existing.mkdir(); (existing / 'keep').write_text('original')
    with pytest.raises(FileExistsError): pipeline().diagnose(db, 'stage2', existing, api)
    assert (existing / 'keep').read_text() == 'original'
    for out in [db.parent / 'nested', tmp_path / 'raw' / 'nested']:
        with pytest.raises(ValueError): pipeline().diagnose(db, 'stage2', out, api)
        assert not out.exists()
    for kwargs in [dict(batch='stage1'), dict(batch='stage2', bundle_id='invented'), dict(batch='stage2', limit_bundles=0)]:
        out = tmp_path / 'invalid'
        with pytest.raises(ValueError): pipeline().diagnose(db, output_dir=out, backend=api, **kwargs)
        assert not out.exists()
    assert not api.requests


def test_cli_replay_from_outside_checkout(evidence, tmp_path):
    db, packet = evidence
    fixture = tmp_path / 'responses.jsonl'
    fixture.write_text(''.join(json.dumps(e) + '\n' for e in replay_entries(db, packet)))
    out = tmp_path / 'cli'
    p = subprocess.run([sys.executable, '-m', 'aiops_v4.agents', 'diagnose', '--database', str(db), '--batch', 'stage2',
         '--output-dir', str(out), '--backend', 'replay', '--model', 'fixture-model', '--replay-file', str(fixture),
         '--bundle-id', packet['bundle_id']], cwd=tmp_path, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['predictions'] == 1
    assert rows(out / 'predictions.jsonl')


def test_failed_role_calls_are_counted_and_unreported_usage_is_explicit(evidence, tmp_path):
    db, packet = evidence
    api = ReplayBackend('fixture-model', [])
    summary = pipeline().diagnose(db, 'stage2', tmp_path / 'usage', api, bundle_id=packet['bundle_id'])
    assert summary['backend_calls'] == 1 and summary['usage_reported_calls'] == 0
    assert summary['usage_complete'] is False


def test_later_malformed_tool_call_preserves_earlier_audit_and_prediction(evidence, tmp_path):
    from aiops_v4.evidence.index import query_evidence
    db, packet = evidence; before = sha(db)
    other = query_evidence(db, 'stage2', 'bundle', entity_id='xian-br-2')[0]
    replies = replay_entries(db, packet) + [dict(role='confirmation', case_id=other['bundle_id'], turn=0,
        message={'role': 'assistant', 'content': None, 'tool_calls': [dict(id='bad', type='function',
            function={'name': 'query_states', 'arguments': '{"start":123}'})]})]
    out = tmp_path / 'later-failure'
    summary = pipeline().diagnose(db, 'stage2', out, ReplayBackend('fixture-model', replies))
    assert summary['predictions'] == 1 and summary['bundles_deferred'] == 1
    assert summary['selection_complete'] and not summary['diagnosis_complete']
    assert len(rows(out / 'roles.jsonl')) == 5 and len(rows(out / 'predictions.jsonl')) == 1
    assert rows(out / 'bundles.jsonl')[-1]['error_code'] == 'invalid_tool_query'
    assert rows(out / 'roles.jsonl')[-1]['status'] == 'deferred' and sha(db) == before


@pytest.mark.parametrize('role', ['confirmation', 'localization', 'classification'])
def test_huge_confidence_defers_role_and_publishes_audit(evidence, tmp_path, role):
    db, packet = evidence
    replies = replay_entries(db, packet)
    entry = next(e for e in replies if e['role'] == role)
    output = json.loads(entry['message']['content'])
    if role == 'confirmation': output['assessments'][0]['confidence'] = 10 ** 400
    elif role == 'localization': output['candidates'][0]['confidence'] = 10 ** 400
    else: output['confidence'] = 10 ** 400
    entry['message']['content'] = json.dumps(output)
    out = tmp_path / 'huge-confidence'
    summary = pipeline().diagnose(db, 'stage2', out, ReplayBackend('fixture-model', replies), bundle_id=packet['bundle_id'])
    assert summary['predictions'] == 0 and not summary['diagnosis_complete']
    run = next(r for r in rows(out / 'roles.jsonl') if r['role'] == role)
    assert run['status'] == 'deferred' and run['error_code'] == 'invalid_role_contract'
    assert run['trace'] and (out / 'summary.json').is_file()


@pytest.mark.parametrize('transport', ['bad_status', 'chunked'])
def test_malformed_http_transport_is_deferred_with_sanitized_published_audit(evidence, tmp_path, server, transport):
    from aiops_v4.agents.backend import HTTPBackend
    db, packet = evidence; url, captured, reply = server
    reply['transport'] = transport
    out = tmp_path / 'http-failure'
    summary = pipeline().diagnose(db, 'stage2', out, HTTPBackend('fixture-model', url, 'test-secret', timeout=2), bundle_id=packet['bundle_id'])
    assert summary['bundles_deferred'] == 1 and summary['predictions'] == 0 and len(captured) == 1
    assert rows(out / 'roles.jsonl')[0]['error_code'] == 'network_error'
    assert summary['backend_calls'] == 1 and summary['usage_complete'] is False
    for artifact in out.iterdir():
        text = artifact.read_text()
        assert 'test-secret' not in text and 'secret-server-text' not in text

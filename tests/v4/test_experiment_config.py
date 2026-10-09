"""Reject leaked labels/config ambiguity; do not fabricate costs or provenance."""
import importlib
import json
from pathlib import Path
import pytest


def config(raw='raw', replay='responses.jsonl'):
    return dict(schema_version=1, batch='stage2', raw_root=str(raw), naive_timezone='UTC',
                backend=dict(kind='replay', model='fixture-model', replay_file=str(replay)))


def api():
    return importlib.import_module('aiops_v4.experiments.config')


def test_config_resolves_paths_and_sets_hybrid_independent_defaults(tmp_path):
    p = tmp_path / 'config.json'; p.write_text(json.dumps(config()))
    result = api().load_config(p)
    assert result['raw_root'] == str(tmp_path / 'raw')
    assert result['backend']['replay_file'] == str(tmp_path / 'responses.jsonl')
    assert result['states']['mode'] == 'hybrid' and result['states']['rules_enabled'] is True
    assert result['agents']['mode'] == 'independent' and result['agents']['review_mode'] == 'llm'
    assert result['agents']['knowledge'] is True and result['agents']['state_explanation'] is False


@pytest.mark.parametrize('mutation', [
    lambda c: c.update(ground_truth='answers.jsonl'),
    lambda c: c['backend'].update(api_key='secret'),
    lambda c: c.update(profile={'max_rows_per_file': True}),
    lambda c: c.update(states={'clusters': 9}),
    lambda c: c.update(states={'rare_fraction': float('nan')}),
    lambda c: c.update(states={'reference_start':'2026-01-01T00:00:00Z'}),
    lambda c: c.update(features={'window_seconds':60,'expected_step_seconds':7}),
    lambda c: c.update(agents={'budget':{'max_turns':0}}),
    lambda c: c.update(evidence={'context_seconds':86401}),
    lambda c: c.update(evidence={'observations_per_kind':33}),
    lambda c: c['backend'].update(kind='http',base_url='https://secret@host',api_key_env='KEY'),
    lambda c: c.update(pricing={'currency':'USD','input_per_million':-1,'output_per_million':2}),
])
def test_config_preflight_rejects_unknown_secret_and_invalid_scope(tmp_path, mutation):
    c=config(); mutation(c)
    with pytest.raises(ValueError): api().validate_config(c, tmp_path)


def test_duplicate_json_fields_are_not_silently_overwritten(tmp_path):
    p=tmp_path/'bad.json'; p.write_text('{"batch":"a","batch":"b"}')
    with pytest.raises(ValueError): api().load_config(p)


def test_knowledge_is_public_taxonomy_with_limits_not_per_event_answers():
    from aiops_challenge_2026.schema import VALID_MAJOR_SUB_PAIRS
    cards = importlib.import_module('aiops_v4.experiments.knowledge').knowledge_cards()
    assert {(c['major_category'],c['sub_category']) for c in cards['cards']} == VALID_MAJOR_SUB_PAIRS
    assert len(cards['cards']) == 32 and cards['source'].startswith('https://challenge.aiops.cn/')
    assert cards['limits'] and cards['purpose'] and cards['version']
    for card in cards['cards']:
        assert card['mechanism'] and card['verification_hypotheses']
        assert 'start_time' not in card and 'root_cause' not in card and 'network_element_id' not in card
        assert card['hypothesis_status'] == 'generic_engineering_not_official_test_evidence'


def test_costs_require_real_usage_and_explicit_prices():
    records=importlib.import_module('aiops_v4.experiments.records')
    s=dict(simulated=False,backend_calls=2,usage_reported_calls=2,usage_complete=True,
           usage={'prompt_tokens':1000000,'completion_tokens':500000,'total_tokens':1500000})
    p=dict(currency='USD',input_per_million=2,output_per_million=4)
    assert records.estimate_cost(s,p)['estimated_cost'] == 4
    assert records.estimate_cost(s,None)['estimated_cost'] is None
    s['usage_complete']=False
    assert records.estimate_cost(s,p)['estimated_cost'] is None
    assert records.estimate_cost(s,p)['known_usage_cost'] == 4
    s['simulated']=True
    assert records.estimate_cost(s,p)['estimated_cost'] is None
    assert records.estimate_cost(s,p)['known_usage_cost'] is None
    assert records.estimate_cost(s,p)['reason'] == 'simulated_not_billable'


def test_provenance_hashes_all_prompts_and_source_without_credentials(tmp_path):
    records=importlib.import_module('aiops_v4.experiments.records')
    result=records.provenance(api().validate_config(config(),tmp_path))
    assert len(result['config_sha256']) == len(result['code_sha256']) == len(result['knowledge_sha256']) == 64
    assert set(result['prompt_sha256']) >= {'confirmation','localization','classification','review'}
    assert result['python'] and result['platform'] and result['git_commit']
    assert 'environment' not in result

import importlib
import json
from pathlib import Path
import pytest
from test_agent_tools import evidence
from test_agent_pipeline import replay_entries, rows
from test_evidence_pipeline import inputs, sha
from test_experiment_config import config
from aiops_v4.agents.backend import ReplayBackend


def api(): return importlib.import_module('aiops_v4.experiments.run')


def fixture_config(raw):
    c=config(raw)
    c['states']=dict(reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z')
    return c


def test_raw_to_official_replay_preserves_inputs_and_seals_every_artifact(evidence,tmp_path):
    db,packet=evidence;raw=tmp_path/'raw';before={str(p):sha(p) for p in raw.rglob('*.csv')}
    c=fixture_config(raw);c['agents']=dict(bundle_id=packet['bundle_id'])
    backend=ReplayBackend('fixture-model',replay_entries(db,packet));out=tmp_path/'run'
    summary=api().run_experiment(c,out,backend=backend)
    assert summary['status']=='completed' and summary['predictions']==1 and summary['simulated']
    assert summary['partial'] and not summary['diagnosis_complete']
    assert not summary['valid_for_model_quality'] and not summary['submission_ready']
    assert summary['cost']['estimated_cost'] is None
    assert summary['stages']['profile']['rows_scanned']==summary['stages']['features']['rows_scanned']
    assert summary['stage_elapsed_seconds'].keys() >= {'profile','features','states','evidence','agents'}
    assert {str(p):sha(p) for p in raw.rglob('*.csv')}==before
    for relative,digest in summary['artifact_sha256'].items(): assert sha(out/relative)==digest
    assert api().verify_run(out)['batch']=='stage2'
    assert rows(out/'agents'/'predictions.jsonl')[0]['fault_category']['major_category']=='routing'
    assert (out/'experiment.json').is_file() and (out/'config.json').is_file()


def test_prefix_and_discovery_only_are_not_full_diagnoses(tmp_path):
    raw,_,_=inputs(tmp_path,interfaces=0,spike=False)
    c=fixture_config(raw);c['profile']=dict(max_rows_per_file=2);c['agents']=dict(discovery_only=True)
    summary=api().run_experiment(c,tmp_path/'prefix')
    assert summary['partial'] and summary['input_scope']['mode']=='prefix_sample'
    assert summary['predictions']==0 and not summary['diagnosis_complete']
    assert summary['backend_calls']==0 and not summary['submission_ready']
    assert (tmp_path/'prefix'/'agents'/'predictions.jsonl').read_text()==''


def test_preflight_does_not_mutate_existing_or_raw_outputs(evidence,tmp_path):
    db,packet=evidence;c=fixture_config(tmp_path/'raw');backend=ReplayBackend('fixture-model',[])
    keep=tmp_path/'keep';keep.mkdir();(keep/'file').write_text('original')
    with pytest.raises(FileExistsError):api().run_experiment(c,keep,backend=backend)
    with pytest.raises(ValueError):api().run_experiment(c,tmp_path/'raw'/'nested',backend=backend)
    c['ground_truth']='forbidden'
    with pytest.raises(ValueError):api().run_experiment(c,tmp_path/'new',backend=backend)
    assert not (tmp_path/'new').exists() and not backend.requests and (keep/'file').read_text()=='original'


def test_raw_parse_failure_has_failure_marker_and_no_success(tmp_path):
    raw=tmp_path/'raw';raw.mkdir();(raw/'node_metrics.csv').write_bytes(b'\xff\xff')
    c=fixture_config(raw);c['agents']=dict(discovery_only=True);out=tmp_path/'failed'
    with pytest.raises(ValueError):api().run_experiment(c,out)
    assert not (out/'summary.json').exists()
    failure=json.loads((out/'failure.json').read_text());assert failure['stage']=='profile'
    assert failure['status']=='failed' and 'message' not in failure
    with pytest.raises(ValueError):api().verify_run(out)


def test_seal_detects_artifact_changes_before_evaluation(tmp_path):
    raw,_,_=inputs(tmp_path,interfaces=0,spike=False)
    c=fixture_config(raw);c['agents']=dict(discovery_only=True);out=tmp_path/'sealed'
    api().run_experiment(c,out)
    (out/'states'/'models.jsonl').write_text('{}\n')
    with pytest.raises(ValueError):api().verify_run(out)


def test_failed_calls_and_unreported_usage_are_recorded_as_unknown(evidence,tmp_path):
    db,packet=evidence;c=fixture_config(tmp_path/'raw');c['agents']=dict(bundle_id=packet['bundle_id'])
    s=api().run_experiment(c,tmp_path/'unknown',backend=ReplayBackend('fixture-model',[]))
    assert s['backend_calls']==1 and s['usage_reported_calls']==0 and not s['usage_complete']
    assert s['stages']['agents']['bundles_deferred']==1 and not s['diagnosis_complete']

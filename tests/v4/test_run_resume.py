import json
import shutil
from types import SimpleNamespace

import pytest

from test_evidence_pipeline import inputs
from test_experiment_run import fixture_config
from aiops_v4.experiments import run


def offline_config(raw):
    c = fixture_config(raw)
    c['agents'] = dict(discovery_only=True)
    return c


def interrupt_before_evidence(monkeypatch, tmp_path):
    raw, _, _ = inputs(tmp_path, interfaces=0, spike=False)
    c = offline_config(raw)
    output = tmp_path/'experiment'
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()
    with monkeypatch.context() as patch:
        patch.setattr(run, 'build_evidence', interrupt)
        with pytest.raises(KeyboardInterrupt):
            run.run_experiment(c, output)
    return c, output


def test_interrupted_run_resumes_verified_completed_stages_without_raw_rescan(tmp_path, monkeypatch):
    c, output = interrupt_before_evidence(monkeypatch, tmp_path)
    original = (output/'features'/'windows.jsonl').read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('completed raw stage was repeated')
    monkeypatch.setattr(run, 'profile_dataset', forbidden)
    monkeypatch.setattr(run, 'build_features', forbidden)
    summary = run.run_experiment(c, output, resume=True)
    assert summary['status'] == 'completed'
    assert (output/'features'/'windows.jsonl').read_bytes() == original
    assert not (output/'failure.json').exists()
    assert summary['resumed_stages'] == ['profile', 'features', 'states']
    assert run.verify_run(output)['input_scope']['complete'] is True


def test_resume_rejects_changed_producer_artifacts(tmp_path, monkeypatch):
    c, output = interrupt_before_evidence(monkeypatch, tmp_path)
    (output/'states'/'events.jsonl').write_text('{}\n')
    with pytest.raises(ValueError):
        run.run_experiment(c, output, resume=True)
    assert (output/'failure.json').exists()
    assert not (output/'summary.json').exists()


def test_resume_rejects_changed_configuration_or_raw_manifest(tmp_path, monkeypatch):
    c, output = interrupt_before_evidence(monkeypatch, tmp_path)
    c['profile'] = dict(max_rows_per_file=2)
    with pytest.raises(ValueError):
        run.run_experiment(c, output, resume=True)
    assert not (output/'summary.json').exists()


def test_disk_reserve_preflight_leaves_no_partial_run(tmp_path):
    raw, _, _ = inputs(tmp_path, interfaces=0, spike=False)
    output=tmp_path/'experiment'
    free=shutil.disk_usage(tmp_path).free
    with pytest.raises(ValueError,match='disk'):
        run.run_experiment(offline_config(raw), output, min_free_bytes=free+1024**3)
    assert not output.exists()


def test_progress_reports_complete_stage_boundaries_and_raw_scan_counts(tmp_path):
    raw, _, _ = inputs(tmp_path, interfaces=0, spike=False)
    notifications=[]
    summary=run.run_experiment(offline_config(raw),tmp_path/'experiment',progress=notifications.append)
    complete=[p['stage'] for p in notifications if p['event']=='stage_complete']
    assert complete==['profile','features','states','evidence','agents']
    assert any(p.get('event')=='file_done' and p.get('rows',0)>0 for p in notifications)
    assert all(p['disk_free_bytes']>=0 for p in notifications)
    assert summary['status']=='completed'


def test_low_disk_is_not_swallowed_as_a_bad_csv_and_checkpointed(tmp_path,monkeypatch):
    raw=tmp_path/'raw';raw.mkdir()
    path=raw/'node_metrics.csv'
    path.write_text('timestamp,region,node,cpu_usage\n'+
        '2026-09-17T04:00:00Z,xian,br-1,10\n'*10001)
    checks=[]
    def free_space(_):
        checks.append(None)
        # preflight, stage start, file start, then the in-loop 10k-row check.
        return SimpleNamespace(free=0 if len(checks)==4 else 100)
    monkeypatch.setattr(run.shutil,'disk_usage',free_space)
    output=tmp_path/'experiment'
    with pytest.raises(ValueError):
        run.run_experiment(offline_config(raw),output,min_free_bytes=1,progress=lambda _:None)
    failure=json.loads((output/'failure.json').read_text())
    assert failure['stage']=='profile' and failure['completed_stages']==[]
    assert not (output/'profile').exists()

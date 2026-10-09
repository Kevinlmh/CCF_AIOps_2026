"""Offline handoff: no provider setup, plus safe interrupted publication."""
import json
from pathlib import Path
import pytest
from test_agent_tools import evidence
from test_experiment_run import fixture_config
from test_evidence_pipeline import inputs
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.experiments.config import load_config
from aiops_v4.experiments import run


def test_ctrl_c_records_interruption_without_success_or_exception_text(evidence,tmp_path):
    db,packet=evidence
    class InterruptingReplay(ReplayBackend):
        def complete(self,*args,**kwargs):raise KeyboardInterrupt('sensitive interruption detail')
    config=fixture_config(tmp_path/'raw');config['agents']=dict(bundle_id=packet['bundle_id'])
    out=tmp_path/'interrupted'
    with pytest.raises(KeyboardInterrupt):run.run_experiment(config,out,backend=InterruptingReplay('fixture-model',[]))
    failure=json.loads((out/'failure.json').read_text())
    assert failure['status']=='interrupted' and failure['error_type']=='KeyboardInterrupt'
    assert failure['stage']=='agents' and failure['completed_stages']==['profile','features','states','evidence']
    assert 'sensitive interruption detail' not in (out/'failure.json').read_text()
    assert not (out/'summary.json').exists() and not (out/'seal.json').exists()
    assert (out/'evidence'/'evidence.sqlite').is_file()
    with pytest.raises(ValueError):run.verify_run(out)


@pytest.mark.parametrize('batch',['stage1','stage2'])
def test_shipped_offline_configuration_never_constructs_a_model_backend(tmp_path,monkeypatch,batch):
    raw,_,_=inputs(tmp_path,interfaces=0,spike=False)
    path=Path(__file__).resolve().parents[2]/'configs'/'v4'/(batch+'-offline.json')
    config=load_config(path)
    assert config['agents']['discovery_only'] and config['profile']['max_rows_per_file'] is None
    assert config['backend']['kind']=='replay'
    # No fake replies are loaded or used, even if the configured placeholder is absent.
    config['raw_root']=str(raw)
    config['backend']['replay_file']=str(tmp_path/'does-not-exist.jsonl')
    def forbidden(*args,**kwargs):raise AssertionError('offline path tried to construct a provider')
    monkeypatch.setattr(run,'make_backend',forbidden)
    monkeypatch.setattr('aiops_v4.experiments.config.HTTPBackend',forbidden)
    summary=run.run_experiment(config,tmp_path/'offline')
    assert summary['status']=='completed' and summary['discovery_only']
    assert summary['backend_calls']==0 and summary['external_inputs']==[]
    assert summary['predictions']==0 and not summary['submission_ready']
    assert (tmp_path/'offline'/'evidence'/'evidence.sqlite').is_file()

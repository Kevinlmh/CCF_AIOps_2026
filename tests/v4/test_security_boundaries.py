"""Concrete audit regressions at raw input and model tool boundaries."""
import json
import os
from pathlib import Path
import pytest
from test_agent_tools import evidence
from test_evidence_pipeline import inputs
from test_records import write_csv, read
from test_experiment_run import fixture_config
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.agents.engine import run_role
from aiops_v4.agents.tools import EvidenceSession
from aiops_v4.data.discovery import discover_sources
from aiops_v4.evidence.index import query_evidence
from aiops_v4.experiments import run


@pytest.mark.parametrize('args', [dict(offset=2**63),dict(offset=10**400),dict(start='0001-01-01T00:00:00+01:00')])
def test_unrepresentable_tool_queries_are_controlled_deferrals(evidence,args):
    db,packet=evidence
    backend=ReplayBackend('fixture-model',[dict(role='confirmation',case_id='case',turn=0,
        message=dict(role='assistant',content=None,tool_calls=[dict(id='bad',type='function',
            function=dict(name='query_states',arguments=json.dumps(args)))]))])
    result=run_role(backend,EvidenceSession(db,'stage2',packet),'confirmation','case',dict(bundle=packet))
    assert result['status']=='deferred' and result['error_code']=='invalid_tool_query'
    assert result['backend_calls']==1 and result['trace'][0]['tools'][0]['error_code']=='invalid_tool_query'


def test_direct_evidence_query_validates_sqlite_offset(evidence):
    db,_=evidence
    with pytest.raises(ValueError):query_evidence(db,'stage2','states',offset=2**63)


@pytest.mark.parametrize('timestamp',['0001-01-01T00:00:00+01:00','9999-12-31T23:59:59-01:00'])
def test_raw_out_of_range_utc_is_preserved_as_invalid_time(tmp_path,timestamp):
    records,_=read(tmp_path,'node_metrics.csv',[dict(timestamp=timestamp,node='br1',cpu_usage='10')])
    assert records[0].timestamp is None and records[0].raw['timestamp']==timestamp
    assert 'invalid_timestamp' in records[0].quality_flags


@pytest.mark.parametrize('entry',['outside_link','inside_link','directory','fifo'])
def test_discovery_refuses_links_and_non_regular_csv_inputs(tmp_path,entry):
    root=tmp_path/'raw';root.mkdir();path=root/'node_metrics.csv'
    if entry in ('outside_link','inside_link'):
        target=write_csv(tmp_path/'outside' if entry=='outside_link' else root,'node_metrics-target.csv',
            [dict(timestamp='2026-09-17T04:00:00Z',node='xian-br-1',cpu_usage='10')])
        path.symlink_to(target)
    elif entry=='directory':path.mkdir()
    else:os.mkfifo(path)  # Never open the FIFO; discovery must reject before I/O.
    with pytest.raises(ValueError):discover_sources(root)


def test_cross_scan_content_change_cannot_hide_behind_same_size_and_mtime(tmp_path,monkeypatch):
    raw,_,_=inputs(tmp_path,interfaces=0,spike=False)
    path=next(raw.rglob('node_metrics.csv'));before=path.stat()
    config=fixture_config(raw);config['agents']=dict(discovery_only=True)
    original=run.profile_dataset
    def changing_profile(*args,**kwargs):
        profile=original(*args,**kwargs)
        content=path.read_bytes();changed=content.replace(b',10',b',20')
        assert changed!=content and len(changed)==len(content)
        path.write_bytes(changed);os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        return profile
    monkeypatch.setattr(run,'profile_dataset',changing_profile)
    out=tmp_path/'run'
    with pytest.raises(ValueError):run.run_experiment(config,out)
    failure=json.loads((out/'failure.json').read_text())
    assert failure['stage']=='features' and not (out/'summary.json').exists()

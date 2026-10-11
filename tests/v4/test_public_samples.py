import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('v4_public_samples',Path(__file__).resolve().parents[2]/'scripts/v4_public_samples.py')
samples=importlib.util.module_from_spec(spec);spec.loader.exec_module(samples)


def test_public_resume_rejects_different_code_and_raw_file_snapshot(tmp_path):
    from test_experiment_config import config
    from aiops_v4.experiments.run import run_experiment
    from test_evidence_pipeline import inputs
    raw,_,_=inputs(tmp_path,interfaces=0,spike=False)
    c=config(raw);c['agents']={'discovery_only':True}
    out=tmp_path/'run';s=run_experiment(c,out)
    samples.validate_reuse(s,c)
    old=dict(s,provenance=dict(s['provenance'],code_sha256='old-code'))
    with pytest.raises(ValueError,match='code'):samples.validate_reuse(old,c)
    file=next(raw.rglob('node_metrics.csv'));file.write_text(file.read_text()+'\n')
    with pytest.raises(ValueError,match='raw'):samples.validate_reuse(s,c)


def test_case_scoring_keeps_each_supplied_truth_and_all_predictions():
    truth=[json.loads(line) for line in (samples.REPO/'sample/ground_truth.jsonl').read_text().splitlines()]
    scores=samples.score_public_cases(truth,{case:[] for case in samples.CASE_TRUTH_IDS})
    assert all(row['FN']==1 and row['Total']==0 for row in scores.values())


def test_cached_evidence_keeps_producer_and_diagnoser_separate(tmp_path):
    from test_experiment_config import config
    from test_evidence_pipeline import inputs,sha
    from test_agent_pipeline import replay_entries
    from aiops_v4.experiments.run import run_experiment
    from aiops_v4.agents.backend import ReplayBackend
    from aiops_v4.evidence.index import query_evidence
    raw,_,_=inputs(tmp_path,interfaces=0,spike=True)
    c=config(raw);c['agents']={'discovery_only':True}
    source=tmp_path/'source';run_experiment(c,source)
    db=source/'evidence/evidence.sqlite';before=sha(db)
    entries=[]
    for packet in query_evidence(db,'stage2','bundle'):entries.extend(replay_entries(db,packet))
    c['agents']['discovery_only']=False
    out=tmp_path/'new'
    s=samples.cached_case(c,source,out,backend=ReplayBackend('fixture-model',entries))
    assert s['input_scope']['complete'] and s['stages']['agents']['selection_complete']
    assert s['input_reused'] and s['source_run']==str(source.resolve())
    assert sha(db)==before and not (out/'features').exists()
    assert samples.cached_case(c,source,out,backend=ReplayBackend('fixture-model',[]))==s
    (out/'agents/predictions.jsonl').write_text('tampered')
    with pytest.raises(ValueError):samples.cached_case(c,source,out)


def test_cached_evidence_rejects_changed_detector_configuration(tmp_path):
    from test_experiment_config import config
    from test_evidence_pipeline import inputs
    from aiops_v4.experiments.run import run_experiment
    raw,_,_=inputs(tmp_path,interfaces=0,spike=False)
    c=config(raw);c['agents']={'discovery_only':True}
    source=tmp_path/'source';run_experiment(c,source)
    c['states']={'stat_threshold':9}
    with pytest.raises(ValueError,match='configuration'):samples.cached_case(c,source,tmp_path/'new')

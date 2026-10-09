import importlib
import json
import pytest
from test_agent_tools import evidence
from test_agent_pipeline import replay_entries, rows
from test_evidence_pipeline import inputs, sha
from test_experiment_run import fixture_config
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.experiments.run import run_experiment


def evaluation():return importlib.import_module('aiops_v4.experiments.evaluation')
def ablation():return importlib.import_module('aiops_v4.experiments.ablation')


def prepared(evidence,tmp_path):
    db,packet=evidence;c=fixture_config(tmp_path/'raw');c['agents']=dict(bundle_id=packet['bundle_id'])
    out=tmp_path/'run';run_experiment(c,out,backend=ReplayBackend('fixture-model',replay_entries(db,packet)))
    pred=rows(out/'agents'/'predictions.jsonl')[0]
    truth=dict(ground_truth_id='fixture_truth',start_time='2026-09-17T04:12:00.000000Z',end_time='2026-09-17T04:14:00.000000Z',
               root_cause={'network_element_id':'xian-br-1'},fault_category={'major_category':'routing','sub_category':'bgp_session_down'})
    assert pred['start_time']==truth['start_time'] and pred['end_time']==truth['end_time']
    path=tmp_path/'truth.jsonl';path.write_text(json.dumps(truth)+'\n')
    return out,path


def test_post_inference_evaluation_reuses_official_scoring_and_marks_simulated(evidence,tmp_path):
    out,truth=prepared(evidence,tmp_path);before=sha(out/'summary.json')
    result=evaluation().evaluate_run(out,truth,'stage2',tmp_path/'evaluation',allow_partial=True)
    assert result['scores']['Total']==100 and result['scores']['TP']==1 and result['scores']['FP']==0
    assert not result['valid_for_model_quality'] and result['evaluation_kind']=='simulated_contract_check'
    assert result['errors'][0]['root_cause_rank']==1 and result['errors'][0]['start_error_seconds']==0
    assert result['truth_sha256']==sha(truth) and sha(out/'summary.json')==before
    assert 'accuracy' not in result and (tmp_path/'evaluation'/'report.md').is_file()


def test_error_analysis_uses_real_matching_and_explicit_fp_fn():
    truth=[dict(ground_truth_id='g',start_time='2026-01-01T00:00:00Z',end_time='2026-01-01T00:10:00Z',
        root_cause={'network_element_id':'xian-br-1'},fault_category={'major_category':'routing','sub_category':'bgp_session_down'})]
    prediction=[dict(prediction_id='p',start_time='2026-01-01T00:01:00Z',end_time='2026-01-01T00:11:00Z',
        root_cause_top5=[{'rank':1,'network_element_id':'xian-br-2'}],fault_category={'major_category':'resource','sub_category':'cpu_pressure'}),
        dict(prediction_id='fp',start_time='2026-01-02T00:00:00Z',end_time='2026-01-02T00:01:00Z',
        root_cause_top5=[],fault_category={'major_category':'link','sub_category':'loss'})]
    result=evaluation().analyze_records(truth,prediction)
    e=result['errors'][0]
    assert result['scores']['TP']==1 and result['scores']['FP']==1
    assert e['start_error_seconds']==60 and e['end_error_seconds']==60
    assert e['root_cause_rank'] is None and e['major_correct'] is False and e['minor_correct'] is False
    assert result['errors'][-1]['kind']=='FP' and result['errors'][-1]['prediction_id']=='fp'
    no_pred=evaluation().analyze_records(truth,[])
    assert no_pred['scores']['FN']==1 and no_pred['errors'][0]['kind']=='FN'


def test_partial_wrong_batch_and_modified_artifacts_refuse_before_truth_read(evidence,tmp_path):
    out,truth=prepared(evidence,tmp_path)
    with pytest.raises(ValueError):evaluation().evaluate_run(out,tmp_path/'does-not-exist','stage2',tmp_path/'partial')
    with pytest.raises(ValueError):evaluation().evaluate_run(out,truth,'stage1',tmp_path/'foreign',allow_partial=True)
    assert not (tmp_path/'partial').exists() and not (tmp_path/'foreign').exists()
    (out/'agents'/'predictions.jsonl').write_text('forged')
    with pytest.raises(ValueError):evaluation().evaluate_run(out,tmp_path/'does-not-exist','stage2',tmp_path/'changed',allow_partial=True)
    assert not (tmp_path/'changed').exists()


def test_ablation_produces_independent_modes_and_no_fake_model_predictions(tmp_path):
    raw,_,_=inputs(tmp_path,interfaces=1);c=fixture_config(raw)
    replay=tmp_path/'replay.jsonl';replay.write_text('');c['backend']['replay_file']=str(replay)
    out=tmp_path/'ablation'
    result=ablation().run_ablation(c,out)
    assert set(result['runs'])=={'full','statistics_only','cluster_only','no_rules','joint_roles','program_review','no_knowledge','with_state_explanation','discovery_only'}
    summaries={name:json.loads((out/name/'summary.json').read_text()) for name in result['runs']}
    assert summaries['statistics_only']['stages']['states']['config']['mode']=='statistics'
    assert summaries['cluster_only']['stages']['states']['config']['mode']=='cluster'
    assert summaries['no_rules']['stages']['states']['config']['rules_enabled'] is False
    assert summaries['joint_roles']['stages']['agents']['diagnostic_mode']=='joint'
    assert summaries['program_review']['stages']['agents']['semantic_review'] is False
    assert summaries['discovery_only']['backend_calls']==0 and not summaries['discovery_only']['submission_ready']
    assert all(s['stage_elapsed_seconds']['features']>0 for s in summaries.values())
    assert len({s['input_fingerprint'] for s in summaries.values()})==1
    comparison=ablation().compare_runs([out/'full',out/'no_rules'],tmp_path/'comparison')
    assert comparison['input_comparable'] is True and len(comparison['runs'])==2
    assert comparison['scores_available'] is False


def test_comparison_does_not_merge_different_batches_or_prefixes(tmp_path):
    raw,_,_=inputs(tmp_path,interfaces=0,spike=False);c=fixture_config(raw);c['agents']=dict(discovery_only=True)
    first=tmp_path/'first';run_experiment(c,first)
    c['profile']=dict(max_rows_per_file=2);second=tmp_path/'second';run_experiment(c,second)
    result=ablation().compare_runs([first,second],tmp_path/'comparison')
    assert result['input_comparable'] is False and result['comparability_reasons']


def test_comparison_links_only_sealed_scores_for_the_same_truth(evidence,tmp_path):
    first,truth=prepared(evidence,tmp_path);db,packet=evidence
    c=fixture_config(tmp_path/'raw');c['agents']=dict(bundle_id=packet['bundle_id'])
    second=tmp_path/'second';run_experiment(c,second,backend=ReplayBackend('fixture-model',replay_entries(db,packet)))
    one=tmp_path/'eval-one';two=tmp_path/'eval-two'
    evaluation().evaluate_run(first,truth,'stage2',one,allow_partial=True)
    evaluation().evaluate_run(second,truth,'stage2',two,allow_partial=True)
    result=ablation().compare_runs([first,second],tmp_path/'score-comparison',evaluation_dirs=[one,two])
    assert result['scores_available'] and result['scores_comparable']
    assert [r['scores']['Total'] for r in result['runs']]==[100,100]
    (two/'evaluation.json').write_text('{}')
    with pytest.raises(ValueError):ablation().compare_runs([first,second],tmp_path/'tampered-comparison',evaluation_dirs=[one,two])

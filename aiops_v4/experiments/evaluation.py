"""Ground truth is read only here, after inference completion and digest checks."""
from pathlib import Path
from aiops_challenge_2026.schema import load_jsonl,validate_ground_truth,validate_prediction,ensure_unique,parse_utc
from aiops_challenge_2026.evaluator.evaluator import evaluate
from aiops_v4.agents.pipeline import sha256
from aiops_v4.agents.jsonio import dumps,loads
from .run import verify_run,new_output,_write


def analyze_records(truths,predictions):
    # Official loaders add parsed timestamps; remove only those annotations before revalidation.
    public=lambda r: {k:v for k,v in r.items() if k not in ('_start','_end')} if isinstance(r,dict) else r
    truths=[validate_ground_truth(public(t)) for t in truths];predictions=[validate_prediction(public(p)) for p in predictions]
    ensure_unique(truths,'ground_truth_id');ensure_unique(predictions,'prediction_id')
    scores=evaluate(truths,predictions);by_truth={t['ground_truth_id']:t for t in truths};by_pred={p['prediction_id']:p for p in predictions}
    errors=[]
    for detail in scores['per_ground_truth']:
        truth=by_truth[detail['ground_truth_id']]
        if not detail['matched']:
            errors.append(dict(kind='FN',ground_truth_id=truth['ground_truth_id'],prediction_id=None,
                dice=0,start_error_seconds=None,end_error_seconds=None,root_cause_rank=None,major_correct=False,minor_correct=False))
        else:
            pred=by_pred[detail['prediction_id']]
            rank=next((c['rank'] for c in pred['root_cause_top5'] if c['network_element_id']==truth['root_cause']['network_element_id']),None)
            errors.append(dict(kind='matched',ground_truth_id=truth['ground_truth_id'],prediction_id=pred['prediction_id'],
                dice=detail['dice'],start_error_seconds=abs((parse_utc(pred['start_time'])-parse_utc(truth['start_time'])).total_seconds()),
                end_error_seconds=abs((parse_utc(pred['end_time'])-parse_utc(truth['end_time'])).total_seconds()),
                root_cause_rank=rank,major_correct=bool(detail['major']),minor_correct=bool(detail['minor']),
                actual_root=truth['root_cause'],actual_category=truth['fault_category'],predicted_category=pred['fault_category']))
    for identifier in scores['unmatched_prediction_ids']:
        errors.append(dict(kind='FP',prediction_id=identifier,ground_truth_id=None))
    return dict(scores=scores,errors=errors)


def evaluate_run(run_dir,truth,batch,output_dir,*,allow_partial=False):
    if type(allow_partial) is not bool:raise ValueError('allow_partial boolean required')
    root=Path(run_dir).resolve(strict=True)
    summary=verify_run(root)
    if summary['batch']!=batch:raise ValueError('evaluation batch mismatch')
    if summary['discovery_only']:raise ValueError('discovery-only has no model predictions to evaluate')
    if summary['partial'] and not allow_partial:raise ValueError('partial run requires explicit allow_partial')
    truth=Path(truth).resolve(strict=True)
    raw=Path(summary['raw_root']).resolve()
    if truth==raw or raw in truth.parents or root in truth.parents:raise ValueError('truth must be isolated from inference inputs/artifacts')
    identity=truth.stat()
    if any(str(truth)==source['path'] or (identity.st_dev,identity.st_ino)==(source['device'],source['inode'])
           for source in summary['external_inputs']):
        raise ValueError('truth must be isolated from consumed external inference inputs')
    output=new_output(output_dir,(root,raw))
    predictions=load_jsonl(root/'agents'/'predictions.jsonl',validate_prediction)
    # No label content is opened before the sealed run, scope and output preflight.
    truth_before=sha256(truth)
    records=load_jsonl(truth,validate_ground_truth)
    result=analyze_records(records,predictions)
    if sha256(truth)!=truth_before:raise ValueError('truth changed during evaluation')
    if verify_run(root)!=summary:raise ValueError('run changed during evaluation')
    result.update(schema_version=1,batch=batch,run_dir=str(root),run_summary_sha256=sha256(root/'summary.json'),
        truth_sha256=truth_before,truth_scope='entire supplied truth file; no post-hoc label filtering',
        partial=summary['partial'],simulated=summary['simulated'],diagnosis_complete=summary['diagnosis_complete'],
        valid_for_model_quality=summary['valid_for_model_quality'] and not summary['partial'],
        evaluation_kind='simulated_contract_check' if summary['simulated'] else 'partial_model_experiment' if summary['partial'] else 'model_experiment')
    output.mkdir(parents=True,exist_ok=False)
    _write(output/'evaluation.json',result)
    (output/'errors.jsonl').write_text(''.join(dumps(e)+'\n' for e in result['errors']),encoding='utf-8')
    lines=['# v4 独立评测','',f"批次：{batch}；类型：{result['evaluation_kind']}；partial：{result['partial']}。",
        f"可用于完整模型效果：{result['valid_for_model_quality']}；诊断全部完成：{summary['diagnosis_complete']}。",
        '', '模拟回放分数是流程契约检查；部分范围不是全量成绩。全部 supplied truth 保留在分母。',
        '', '| Total | AD | RCA | Major | Minor | TP | FP | FN |','|---:|---:|---:|---:|---:|---:|---:|---:|']
    lines.append('| '+' | '.join(str(result['scores'][k]) for k in ('Total','AD','RCA','Major','Minor','TP','FP','FN'))+' |')
    lines.extend(['','逐条匹配、时间误差、根因名次与类别错误见 errors.jsonl。未匹配真值计 FN，额外预测计 FP。'])
    (output/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    _write(output/'seal.json',dict(artifact_sha256={p.name:sha256(p) for p in output.iterdir() if p.is_file()}))
    return result


def verify_evaluation(directory):
    root=Path(directory).resolve(strict=True)
    seal=loads((root/'seal.json').read_text(encoding='utf-8'))
    expected={'evaluation.json','errors.jsonl','report.md'}
    if set(seal['artifact_sha256'])!=expected or {p.name for p in root.iterdir()}!=expected|{'seal.json'}:
        raise ValueError('evaluation artifact inventory changed')
    if any((root/name).is_symlink() or sha256(root/name)!=digest for name,digest in seal['artifact_sha256'].items()):
        raise ValueError('evaluation artifact changed')
    return loads((root/'evaluation.json').read_text(encoding='utf-8'))

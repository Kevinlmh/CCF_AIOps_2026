"""Independent full runs for explicit ablations; no truth or diagnosis cache reuse."""
from pathlib import Path
from aiops_v4.agents.jsonio import dumps,loads
from aiops_v4.agents.pipeline import sha256
from .config import validate_config
from .run import run_experiment,verify_run,new_output,_write,make_backend
from .records import hash_object

VARIANTS={
 'full':{},
 'statistics_only':dict(states=dict(mode='statistics',rules_enabled=False)),
 'cluster_only':dict(states=dict(mode='cluster',rules_enabled=False)),
 'no_rules':dict(states=dict(rules_enabled=False)),
 'joint_roles':dict(agents=dict(mode='joint')),
 'program_review':dict(agents=dict(review_mode='program_only')),
 'no_knowledge':dict(agents=dict(knowledge=False)),
 'with_state_explanation':dict(agents=dict(state_explanation=True)),
 'discovery_only':dict(agents=dict(discovery_only=True))}


def variant_config(config,name):
    if name not in VARIANTS:raise ValueError('unknown ablation variant')
    result=loads(dumps(config))
    result['states'].update(mode='hybrid',rules_enabled=True)
    result['agents'].update(mode='independent',review_mode='llm',knowledge=True,state_explanation=False,discovery_only=False)
    for section,changes in VARIANTS[name].items():result[section].update(changes)
    return result


def run_ablation(config,output_dir,*,variants=None,replays=None):
    config=validate_config(config)
    names=list(VARIANTS) if variants is None else list(variants)
    if not names or len(set(names))!=len(names) or any(n not in VARIANTS for n in names):raise ValueError('unique known variants required')
    replays=replays or {}
    if not isinstance(replays,dict) or set(replays)-set(names) or (replays and config['backend']['kind']!='replay'):
        raise ValueError('replay overrides must match replay variants')
    output=new_output(output_dir,(config['raw_root'],))
    configs={}
    for name in names:
        c=variant_config(config,name)
        if name in replays:c['backend']['replay_file']=str(Path(replays[name]).resolve(strict=True))
        configs[name]=validate_config(c)
        if not c['agents']['discovery_only']:make_backend(c) # validate credentials/replay before creating suite outputs
    output.mkdir(parents=True,exist_ok=False);runs={}
    try:
        for name in names:
            s=run_experiment(configs[name],output/name)
            runs[name]=dict(run_dir=str(output/name),predictions=s['predictions'],simulated=s['simulated'],
                partial=s['partial'],diagnosis_complete=s['diagnosis_complete'],input_fingerprint=s['input_fingerprint'],
                elapsed_seconds=s['elapsed_seconds'],backend_calls=s['backend_calls'],cost=s['cost'])
        result=dict(schema_version=1,status='completed',batch=config['batch'],runs=runs,
            preprocessing_reused=False,preprocessing_note='Each variant rescans raw data and learns independent states; durations include repeated preprocessing.',
            truth_used=False)
        _write(output/'ablation.json',result)
        return result
    except Exception as error:
        _write(output/'failure.json',dict(status='failed',completed_variants=list(runs),error_type=type(error).__name__))
        raise ValueError('ablation failed; completed runs preserved') from None


def compare_runs(run_dirs,output_dir,*,evaluation_dirs=None):
    roots=[Path(p).resolve(strict=True) for p in run_dirs]
    if len(roots)<2 or len(set(roots))!=len(roots):raise ValueError('at least two distinct runs required')
    summaries=[verify_run(p) for p in roots]
    output=new_output(output_dir,roots+[s['raw_root'] for s in summaries])
    keys=[];rows=[]
    for root,s in zip(roots,summaries):
        c=loads((root/'config.json').read_text(encoding='utf-8'))
        selection={k:c['agents'][k] for k in ('bundle_id','limit_bundles')}
        keys.append((s['batch'],s['input_fingerprint'],hash_object(selection),hash_object(c['features'])))
        rows.append(dict(run_dir=str(root),batch=s['batch'],input_fingerprint=s['input_fingerprint'],
            config_sha256=s['provenance']['config_sha256'],predictions=s['predictions'],simulated=s['simulated'],partial=s['partial'],
            diagnosis_complete=s['diagnosis_complete'],stage_elapsed_seconds=s['stage_elapsed_seconds'],
            elapsed_seconds=s['elapsed_seconds'],backend_calls=s['backend_calls'],cost=s['cost'],
            states=c['states'],agents=c['agents'],scores=None))
    comparable=len(set(keys))==1
    reports=[]
    if evaluation_dirs is not None:
        from .evaluation import verify_evaluation
        directories=list(evaluation_dirs)
        if len(directories)!=len(roots):raise ValueError('one evaluation per run required')
        for row,root,directory in zip(rows,roots,directories):
            report=verify_evaluation(directory)
            if Path(report['run_dir']).resolve()!=root or report['run_summary_sha256']!=sha256(root/'summary.json'):
                raise ValueError('evaluation does not match sealed run')
            row['scores']=report['scores'];row['evaluation_kind']=report['evaluation_kind']
            row['score_valid_for_model_quality']=report['valid_for_model_quality'];reports.append(report)
    result=dict(schema_version=1,input_comparable=comparable,
        comparability_reasons=[] if comparable else ['batch, raw parsed scope, feature configuration or bundle selection differs'],
        scores_available=bool(reports),scores_comparable=bool(reports) and comparable and len({r['truth_sha256'] for r in reports})==1,runs=rows,interpretation='Input-comparable does not mean controlled algorithm comparison; inspect all recorded configuration/model differences. No truth scores inferred.')
    output.mkdir(parents=True,exist_ok=False);_write(output/'comparison.json',result)
    (output/'report.md').write_text('# v4 实验比较\n\n输入范围可比：'+str(comparable)+'。未自动读取评测标签或捏造成绩。\n\n'+
        '| 运行 | 预测 | 调用 | 秒 | 模拟 | 部分 |\n|---|---:|---:|---:|---|---|\n'+
        ''.join(f"| {r['run_dir']} | {r['predictions']} | {r['backend_calls']} | {r['elapsed_seconds']} | {r['simulated']} | {r['partial']} |\n" for r in rows),encoding='utf-8')
    if reports:
        with (output/'report.md').open('a',encoding='utf-8') as stream:
            stream.write('\n成绩范围可比：'+str(result['scores_comparable'])+'；模拟分数不代表真实模型效果。\n\n| 运行 | Total | AD | RCA | Major | Minor |\n|---|---:|---:|---:|---:|---:|\n')
            for row in rows:stream.write('| '+row['run_dir']+' | '+' | '.join(str(row['scores'][k]) for k in ('Total','AD','RCA','Major','Minor'))+' |\n')
    return result

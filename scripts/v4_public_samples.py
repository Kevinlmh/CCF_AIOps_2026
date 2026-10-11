"""Run the entire three public input cases, then read labels for official scoring."""
import argparse
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from aiops_v4.experiments.config import load_config, validate_config
from aiops_v4.experiments.run import run_experiment, verify_run, make_backend, new_output
from aiops_challenge_2026.schema import load_jsonl, validate_ground_truth, validate_prediction
from aiops_challenge_2026.evaluator.evaluator import evaluate
from aiops_v4.experiments.records import provenance
from aiops_v4.data.discovery import discover_sources
from aiops_v4.experiments.records import hash_object
from aiops_v4.experiments.roles import run_diagnosis
from aiops_v4.agents.engine import Budget
from aiops_v4.agents.pipeline import sha256, ARTIFACTS

# The bundled public cases and their supplied answers. Only evaluation uses this
# mapping, after all inference runs; no label fields enter any run configuration.
CASE_TRUTH_IDS = {'case_001': 'incident-0013', 'case_002': 'incident-0017', 'case_003': 'incident-0021'}


def score_public_cases(truth, predictions_by_case):
    by_id = {row['ground_truth_id']: row for row in truth}
    if set(by_id) != set(CASE_TRUTH_IDS.values()):
        raise ValueError('public answer set changed; verify case-to-answer mapping')
    return {case: evaluate([by_id[identifier]], predictions_by_case[case])
            for case, identifier in CASE_TRUTH_IDS.items()}


def validate_raw_snapshot(summary, config):
    c=validate_config(config)
    files={f.relative_path:f for f in discover_sources(Path(c['raw_root']))}
    recorded=summary['stages']['profile']['files']
    if set(files) != {f['path'] for f in recorded}:
        raise ValueError('raw input manifest changed')
    for entry in recorded:
        stat=files[entry['path']].path.stat()
        if stat.st_size != entry['size_bytes'] or stat.st_mtime_ns != entry['mtime_ns']:
            raise ValueError('raw input snapshot changed')


def validate_reuse(summary, config):
    c=validate_config(config)
    if summary['provenance']['code_sha256'] != provenance(c)['code_sha256']:
        raise ValueError('existing run code changed; choose a new output directory')
    validate_raw_snapshot(summary,c)


def cached_case(config, source, output, *, backend=None):
    """Freeze a sealed producer's evidence, run a separately versioned diagnoser.

    This is an inference experiment on historical candidates, not a claim that
    the current detector was rerun. Never copy or edit the source artifacts.
    """
    c=validate_config(config);source=Path(source).resolve(strict=True);output=Path(output).resolve()
    s=verify_run(source);old=json.loads((source/'config.json').read_text())
    if not s['input_scope']['complete'] or s['input_scope']['mode']!='full':
        raise ValueError('cached public diagnosis requires full source input')
    for key in ('batch','raw_root','naive_timezone','profile','features','states','evidence','storage'):
        if c[key]!=old[key]:raise ValueError('cached producer configuration differs')
    validate_raw_snapshot(s,c)
    if output==source or source in output.parents:raise ValueError('output overlaps cached source')
    signature=dict(source=str(source),source_summary_sha256=sha256(source/'summary.json'),
        config_sha256=hash_object(c),diagnoser_code_sha256=provenance(c)['code_sha256'],script_sha256=sha256(__file__))
    record=output/'case_record.json'
    if output.exists():
        seal=json.loads((output/'case_seal.json').read_text())
        if sha256(record)!=seal['record_sha256']:raise ValueError('cached diagnosis record changed')
        item=json.loads(record.read_text())
        if item['signature']!=signature:raise ValueError('cached diagnosis configuration/code/source changed')
        agents=output/'agents'
        if {p.name for p in agents.iterdir()}!=set(ARTIFACTS):raise ValueError('cached diagnosis artifacts changed')
        for name,digest in item['agent_sha256'].items():
            if (agents/name).is_symlink() or sha256(agents/name)!=digest:raise ValueError('cached diagnosis digest mismatch')
        return item['summary']
    output=new_output(output,(source,Path(c['raw_root'])));output.mkdir(parents=True)
    args=dict(c['agents']);args.pop('discovery_only');args['budget']=Budget(**args['budget'])
    a=run_diagnosis(source/'evidence/evidence.sqlite',c['batch'],output/'agents',backend or make_backend(c),**args)
    if sha256(source/'summary.json')!=signature['source_summary_sha256']:
        raise ValueError('cached source changed during diagnosis')
    verify_run(source);validate_raw_snapshot(s,c)
    result=dict(input_reused=True,source_run=str(source),source_provenance=s['provenance'],
        provenance=provenance(c),input_fingerprint=s['input_fingerprint'],input_scope=s['input_scope'],
        stages={**{k:s['stages'][k] for k in ('features','states','evidence')},'agents':a},
        backend_calls=a['backend_calls'],predictions=a['predictions'],simulated=a['simulated'],
        diagnosis_complete=a['diagnosis_complete'])
    item=dict(signature=signature,summary=result,agent_sha256={name:sha256(output/'agents'/name) for name in ARTIFACTS})
    record.write_text(json.dumps(item,ensure_ascii=False,indent=2)+'\n')
    (output/'case_seal.json').write_text(json.dumps(dict(record_sha256=sha256(record)))+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000/v1')
    parser.add_argument('--model', default='Qwen/Qwen3-8B-AWQ')
    parser.add_argument('--api-key-env', default='AIOPS_LOCAL_LLM_KEY')
    parser.add_argument('--discovery-only', action='store_true', help='No score: profile/feature/state/evidence only')
    parser.add_argument('--reuse-evidence-from',type=Path,help='Freeze all three sealed full producer inputs; rerun only diagnosis')
    args = parser.parse_args()
    if args.reuse_evidence_from and args.discovery_only:raise ValueError('cached mode requires diagnosis')
    output = args.output_dir.resolve()
    protected = (REPO/'data', REPO/'sample', REPO/'models')
    if output.is_symlink() or any(output == p or p in output.parents for p in protected):
        raise ValueError('output must be outside inputs')
    output.mkdir(parents=True, exist_ok=True)
    template = load_config(REPO/'configs/v4/conservative-compact.json')
    if args.reuse_evidence_from:
        producers=[verify_run(args.reuse_evidence_from/name) for name in CASE_TRUTH_IDS]
        if len({s['provenance']['code_sha256'] for s in producers})!=1:
            raise ValueError('cached cases have different producer code')
    predictions, cases, predictions_by_case = [], [], {}
    for name in ('case_001', 'case_002', 'case_003'):
        c = json.loads(json.dumps(template))
        c.update(batch='public-'+name, raw_root=str(REPO/'sample'/name))
        c['agents'].update(discovery_only=args.discovery_only)
        if not args.discovery_only:
            c['backend'] = dict(kind='http', model=args.model, base_url=args.base_url, api_key_env=args.api_key_env,
                                timeout=300, max_completion_tokens=1536, enable_thinking=False,temperature=0,structured_roles=True)
        c = validate_config(c)
        root = output/name
        if args.reuse_evidence_from:
            print('复用候选证据，顺序诊断：'+name,flush=True)
            s=cached_case(c,args.reuse_evidence_from/name,root)
        elif root.exists():
            s = verify_run(root)
            if json.loads((root/'config.json').read_text()) != c:
                raise ValueError('existing case config differs; choose another output directory')
            validate_reuse(s,c)
        else:
            print('开始全量运行：'+name, flush=True)
            s = run_experiment(c, root)
        if not s['input_scope']['complete'] or s['input_scope']['mode'] != 'full':
            raise ValueError('public scoring requires full input')
        a = s['stages']['agents']
        record = dict(case=name, rows=s['input_scope']['rows_scanned'], windows=s['stages']['features']['window_count'],
                      candidate_events=s['stages']['states']['candidate_events'], bundles=s['stages']['evidence']['bundles'],
                      backend_calls=s['backend_calls'], predictions=s['predictions'], diagnosis_complete=s['diagnosis_complete'],
                      code_sha256=s['provenance']['code_sha256'], input_fingerprint=s['input_fingerprint'],
                      role_failures=a.get('role_failures', 0), bundles_deferred=a.get('bundles_deferred', 0))
        record['input_reused']=s.get('input_reused',False)
        if record['input_reused']:
            record.update(source_run=s['source_run'],producer_code_sha256=s['source_provenance']['code_sha256'])
        cases.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if not args.discovery_only:
            if s['simulated'] or not a['selection_complete']:
                raise ValueError('public scoring requires real backend and all bundles')
            rows = load_jsonl(root/'agents/predictions.jsonl', validate_prediction)
            predictions_by_case[name] = []
            for row in rows:
                row = {k:v for k,v in row.items() if not k.startswith('_')}
                row['prediction_id'] = name+'-'+row['prediction_id']
                predictions.append(row)
                predictions_by_case[name].append(row)
    result = dict(cases=cases, discovery_only=args.discovery_only)
    if not args.discovery_only:
        merged = output/'predictions.jsonl'
        merged.write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in predictions), encoding='utf-8')
        # First label access happens only after all three complete input runs.
        truth = load_jsonl(REPO/'sample/ground_truth.jsonl', validate_ground_truth)
        result['scores'] = evaluate(truth, predictions)
        result['case_scores'] = score_public_cases(truth, predictions_by_case)
        (output/'official_score.json').write_text(json.dumps(result['scores'], ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        (output/'case_scores.json').write_text(json.dumps(result['case_scores'], ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (output/'public_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

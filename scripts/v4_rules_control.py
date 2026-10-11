"""Resource-only rule control on sealed full public candidates; no LLM calls.

This isolates detection/feature quality from role rejection. It is not the full
v4 diagnoser and makes no claim of covering non-resource fault categories.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from aiops_v4.agents.contracts import load_manifest
from aiops_v4.evidence.index import read_index,query_evidence
from aiops_v4.states.pipeline import _load
from aiops_v4.experiments.run import verify_run
from aiops_v4.agents.pipeline import sha256
from aiops_challenge_2026.schema import load_jsonl,validate_ground_truth,validate_prediction,VALID_NETWORK_ELEMENTS
from aiops_challenge_2026.evaluator.evaluator import evaluate
from scripts.v4_public_samples import score_public_cases,validate_raw_snapshot


def resource_category(windows,reference):
    def value(row,name):
        v=row.get(name)
        return v if type(v) in (int,float) and math.isfinite(v) else None
    def sustained(predicate):
        prior=False
        for row in windows:
            current=predicate(row)
            if current and prior:return True
            prior=current
        return False
    def high(row,name,bound,upper=1):
        v=value(row,name);return v is not None and bound<=v<=upper
    def low(row,name,bound):
        v=value(row,name);return v is not None and 0<=v<=bound
    # Specific resource observations before the less specific CPU/load cue.
    if sustained(lambda r:high(r,'filesystem_used_ratio',.9) or high(r,'inode_used_ratio',.9)):
        return 'disk_space_low'
    if sustained(lambda r:low(r,'memory_available_ratio',.15) or high(r,'swap_used_ratio',.5)):
        return 'memory_pressure'
    if sustained(lambda r:high(r,'disk_io_util',80,100)):
        return 'disk_io_pressure'
    available=value(reference,'memory_available_ratio')
    if available is not None and sustained(lambda r:low(r,'memory_available_ratio',available-.05)):
        return 'memory_pressure'
    cpu,load=value(reference,'cpu_usage'),value(reference,'load1')
    if sustained(lambda r:high(r,'cpu_usage',80,100) or (cpu is not None and load is not None and
            high(r,'cpu_usage',cpu+10,100) and value(r,'load1') is not None and value(r,'load1')>=load+.5)):
        return 'cpu_pressure'
    return None


def predict_case(source):
    source=Path(source).resolve(strict=True);s=verify_run(source)
    if not s['input_scope']['complete']:raise ValueError('full source input required')
    c=json.loads((source/'config.json').read_text());validate_raw_snapshot(s,c)
    database=source/'evidence/evidence.sqlite';before=sha256(database)
    predictions=[];audit=[]
    for packet in query_evidence(database,c['batch'],'bundle',limit=1000):
        if packet['entity_id'] not in VALID_NETWORK_ELEMENTS:
            audit.append(dict(bundle_id=packet['bundle_id'],status='unsupported_entity'));continue
        manifest=load_manifest(database,c['batch'],packet['bundle_id'])
        native=[w for w in manifest if w['source']=='node']
        if len(native)!=len(manifest):
            audit.append(dict(bundle_id=packet['bundle_id'],status='outside_resource_control'));continue
        data=[];reference={}
        with read_index(database,c['batch']) as db:
            for w in native:
                pair=db.execute('SELECT s.payload,m.payload FROM states s JOIN matrices m ON m.id=s.id WHERE s.id=?',(w['vector_id'],)).fetchone()
                state,matrix=map(_load,pair)
                reference=reference or state.get('admission_reference',{})
                data.append({k:v.get('mean') for k,v in matrix['rule_inputs'].items() if v.get('usable')})
        sub=resource_category(data,reference)
        audit.append(dict(bundle_id=packet['bundle_id'],entity_id=packet['entity_id'],category=sub,
            rule_reference=reference,observed_metrics=data,status='predicted' if sub else 'insufficient_rule_support'))
        if sub is None:continue
        start=min(w['start_time'] for w in native);end=max(w['end_time'] for w in native)
        cursor=start
        for w in native:
            if w['start_time']>cursor:raise ValueError('rule control cannot bridge missing intervals')
            cursor=max(cursor,w['end_time'])
        identity=hashlib.sha256(json.dumps(dict(windows=sorted(w['vector_id'] for w in native))).encode()).hexdigest()[:32]
        row=dict(prediction_id='rules-'+identity,start_time=start,end_time=end,
            root_cause_top5=[dict(rank=1,network_element_id=packet['entity_id'])],
            fault_category=dict(major_category='resource',sub_category=sub))
        validate_prediction(row);predictions.append(row)
    if sha256(database)!=before:raise ValueError('input evidence changed')
    return predictions,audit


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-root',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();source=args.evidence_root.resolve(strict=True);output=args.output_dir.resolve()
    if output==source or source in output.parents or any(p==output or p in output.parents for p in (REPO/'data',REPO/'sample',REPO/'models')):
        raise ValueError('output overlaps protected input')
    if output.exists():raise FileExistsError('choose a new output directory')
    all_rows=[];by_case={};audits={}
    for case in ('case_001','case_002','case_003'):
        rows,audit=predict_case(source/case)
        for row in rows:row['prediction_id']=case+'-'+row['prediction_id']
        by_case[case]=rows;all_rows.extend(rows);audits[case]=audit
    # Read supplied answers only after every control prediction has been made.
    truth=load_jsonl(REPO/'sample/ground_truth.jsonl',validate_ground_truth)
    result=dict(control_scope='resource_only; fixed sealed candidates; own-node root; no semantic LLM review',
                backend_calls=0,predictions=len(all_rows),scores=evaluate(truth,all_rows),
                case_scores=score_public_cases(truth,by_case),rules_source_sha256=sha256(__file__))
    output.mkdir(parents=True)
    (output/'predictions.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in all_rows))
    for name,value in (('summary.json',result),('official_score.json',result['scores']),('case_scores.json',result['case_scores']),('rule_audit.json',audits)):
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()

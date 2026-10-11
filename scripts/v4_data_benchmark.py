"""Alternate legacy/canonical data builds, compare semantics, measure private artifacts."""
import argparse
import json
import math
import os
from pathlib import Path
import resource
import shutil
import statistics
import subprocess
import sys
import tarfile
import time

REPO=Path(__file__).resolve().parents[1]


def allocated(root):
    total=0
    for p in Path(root).rglob('*'):
        try:st=p.lstat()
        except FileNotFoundError:continue
        import stat
        if stat.S_ISREG(st.st_mode):total+=st.st_blocks*512
    return total


def worker(args):
    sys.path.insert(0,str(args.source or REPO))
    from aiops_v4.experiments.config import load_config
    from aiops_v4.data.profile import profile_dataset
    from aiops_v4.data.cli import _publish_report
    from aiops_v4.features.pipeline import build_features
    from aiops_v4.states.pipeline import discover_states
    from aiops_v4.evidence.pipeline import build_evidence
    c=load_config((args.source or REPO)/'configs/v4/conservative-compact.json')
    c.update(raw_root=str(args.raw_root.resolve()),batch='benchmark-'+args.raw_root.name)
    output=args.output_dir;output.mkdir(parents=True,exist_ok=False);durations={};started=time.monotonic()
    def stage(name,operation):
        begin=time.monotonic();result=operation();durations[name]=time.monotonic()-begin;return result
    if args.worker=='legacy':
        profile=stage('profile',lambda:profile_dataset(args.raw_root,c['batch'],**c['profile']))
        if profile['errors']:raise ValueError('legacy profile failed')
        _publish_report(profile,output/'profile')
        features=stage('features',lambda:build_features(args.raw_root,c['batch'],output/'features',naive_timezone=c['naive_timezone'],compact=True,**c['features']))
        states=stage('states',lambda:discover_states(output/'features',c['batch'],output/'states',compact=True,**c['states']))
        evidence=stage('evidence',lambda:build_evidence(output/'states',c['batch'],output/'evidence',compact=True,**c['evidence']))
        layers={name:str(output/name) for name in ('features','states','evidence')}
    else:
        from aiops_v4.data.pipeline import build_data_layers
        from aiops_v4.experiments.config import validate_config
        c['storage'].update(backend='canonical',workers=args.workers,min_free_gib=0)
        data=build_data_layers(validate_config(c),output)
        features=data['stages']['features'];states=data['stages']['states'];evidence=data['stages']['evidence'];layers=data['layers'];durations=data['stage_elapsed_seconds']
    elapsed=time.monotonic()-started;self_usage=resource.getrusage(resource.RUSAGE_SELF);children=resource.getrusage(resource.RUSAGE_CHILDREN)
    result=dict(backend=args.worker,case=args.raw_root.name,wall_seconds=elapsed,cpu_seconds=self_usage.ru_utime+self_usage.ru_stime+children.ru_utime+children.ru_stime,
        parent_maxrss_bytes=self_usage.ru_maxrss*(1 if sys.platform=='darwin' else 1024),stage_seconds=durations,
        final_bytes=allocated(output),rows=features['rows_scanned'],windows=features['window_count'],candidate_windows=states['candidate_windows'],
        candidate_events=states['candidate_events'],bundles=evidence['bundles'],backend_calls=0,layers=layers,
        raw_input_bytes=sum(p.stat().st_size for p in args.raw_root.rglob('*.csv')),csv_parse_passes=2 if args.worker=='legacy' else 1)
    print(json.dumps(result),flush=True)


def check_equal(a,b,path='root'):
    if isinstance(a,dict):
        if set(a)!=set(b):raise ValueError('different keys at '+path+': '+str(set(a)^set(b)))
        for k in a:check_equal(a[k],b[k],path+'.'+k)
    elif isinstance(a,(list,tuple)):
        if len(a)!=len(b):raise ValueError('different lengths at '+path)
        for i,(x,y) in enumerate(zip(a,b)):check_equal(x,y,path+f'[{i}]')
    elif isinstance(a,float):
        if type(b) not in (float,int) or not math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-12):raise ValueError('different float at '+path+': '+repr((a,b)))
    elif a!=b or (a is None)!=(b is None):raise ValueError('different value at '+path+': '+repr((a,b)))


def logical_rows(directory,name):
    from aiops_v4.storage import open_jsonl
    with open_jsonl(Path(directory)/(name+'.jsonl'),'rt') as stream:
        for line in stream:
            if line.strip():yield json.loads(line)


def compare(old,new):
    from itertools import zip_longest
    from aiops_v4.features.store import FeatureStore
    from aiops_v4.states.store import StateStore
    from aiops_v4.evidence.index import query_evidence
    counts={}
    def stream(name,left,right):
        n=0
        for a,b in zip_longest(left,right):
            if a is None or b is None:raise ValueError('different record count for '+name)
            check_equal(a,b,name+f'[{n}]');n+=1
        counts[name]=n
    with FeatureStore(Path(new['layers']['features'])/'features.sqlite') as f:
        stream('windows',logical_rows(old['layers']['features'],'windows'),f.windows())
    with StateStore(Path(new['layers']['states'])/'states.sqlite') as s:
        for name in ('models','matrix','states','events'):stream(name,logical_rows(old['layers']['states'],name),s.rows(name))
    def packets(path):
        offset=0
        while True:
            page=query_evidence(Path(path)/'evidence.sqlite','benchmark-'+old['case'],'bundle',limit=100,offset=offset)
            if not page:break
            for p in page:
                p.pop('scoring_provenance',None);yield p
            offset+=len(page)
    stream('packets',packets(old['layers']['evidence']),packets(new['layers']['evidence']))
    return dict(case=old['case'],equivalent=True,records=counts)


def process_rss(pid):
    # RSS of the worker plus descendants; sampled, not an exact kernel peak.
    try:
        rows=subprocess.check_output(['ps','-axo','pid=,ppid=,rss='],text=True).splitlines();values={}
        for line in rows:
            a,b,c=map(int,line.split());values[a]=(b,c*1024)
        owned={pid};changed=True
        while changed:
            extra={a for a,(parent,_) in values.items() if parent in owned};changed=not extra<=owned;owned|=extra
        return sum(values[a][1] for a in owned if a in values)
    except (subprocess.SubprocessError,ValueError,OSError):return None


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',nargs='+',type=Path);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--repeats',type=int,default=3);p.add_argument('--workers',type=int,default=4)
    p.add_argument('--baseline-archive',type=Path);p.add_argument('--worker',choices=('legacy','canonical'));p.add_argument('--raw-root',type=Path);p.add_argument('--source',type=Path)
    args=p.parse_args()
    if args.worker:worker(args);return
    if not args.cases or not 1<=args.repeats<=10 or not 1<=args.workers<=8:raise ValueError('invalid benchmark arguments')
    sys.path.insert(0,str(REPO));root=args.output_dir.resolve();root.mkdir(parents=True,exist_ok=False);source=None
    if args.baseline_archive:
        source=root/'.baseline';source.mkdir()
        with tarfile.open(args.baseline_archive) as archive:
            for member in archive.getmembers():
                if Path(member.name).is_absolute() or '..' in Path(member.name).parts or member.issym() or member.islnk():raise ValueError('unsafe baseline archive')
            archive.extractall(source,**({'filter':'data'} if sys.version_info>=(3,12) else {}))
    runs=[];comparisons=[]
    for repeat in range(args.repeats):
        for case in args.cases:
            pair={}
            for backend in (('legacy','canonical') if repeat%2==0 else ('canonical','legacy')):
                output=root/f'{case.name}_{backend}_{repeat+1}'
                command=[sys.executable,str(Path(__file__).resolve()),'--worker',backend,'--raw-root',str(case.resolve()),'--output-dir',str(output),'--workers',str(args.workers)]
                if backend=='legacy' and source:command+=['--source',str(source)]
                print(json.dumps(dict(event='benchmark_start',case=case.name,backend=backend,repeat=repeat+1)),flush=True)
                log=root/(output.name+'.log');peak=0;rss=0;sample_count=0
                with log.open('w') as stream:
                    proc=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT)
                    while proc.poll() is None:
                        peak=max(peak,allocated(output) if output.exists() else 0);observed=process_rss(proc.pid)
                        if observed is not None:rss=max(rss,observed);sample_count+=1
                        time.sleep(.5)
                    if proc.returncode:raise RuntimeError('benchmark worker failed; see '+str(log))
                value=json.loads(log.read_text().splitlines()[-1]);value.update(repeat=repeat+1,peak_workspace_bytes=max(peak,value['final_bytes']),sampled_tree_peak_rss_bytes=rss,rss_samples=sample_count)
                runs.append(value);pair[backend]=value
                print(json.dumps(dict(event='benchmark_complete',**{k:value[k] for k in ('case','backend','wall_seconds','final_bytes','windows','candidate_events')})),flush=True)
                (root/'progress.json').write_text(json.dumps(dict(runs=runs),indent=2)+'\n')
            comparison=compare(pair['legacy'],pair['canonical']);comparison['repeat']=repeat+1;comparisons.append(comparison)
    medians={}
    for case in args.cases:
        medians[case.name]={}
        for backend in ('legacy','canonical'):
            selected=[r for r in runs if r['case']==case.name and r['backend']==backend]
            medians[case.name][backend]={k:statistics.median(r[k] for r in selected) for k in ('wall_seconds','cpu_seconds','final_bytes','peak_workspace_bytes','sampled_tree_peak_rss_bytes')}
    report=dict(platform=sys.platform,python=sys.version,repeats=args.repeats,workers=args.workers,
        cache_policy='OS cache not flushed; alternating order; shared load not controlled',baseline_source=str(source) if source else 'current legacy backend',
        runs=runs,comparisons=comparisons,medians=medians,accuracy='No model or hidden score measured; exact candidates and bounded-float data equivalence checked.')
    (root/'benchmark.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(dict(report=str(root/'benchmark.json'),equivalent=True)),flush=True)


if __name__=='__main__':main()

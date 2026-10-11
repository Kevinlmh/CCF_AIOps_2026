"""One CSV parse per input, private file spools, canonical minute-store publication."""
from collections import Counter,OrderedDict
from concurrent.futures import ProcessPoolExecutor,as_completed
from itertools import groupby
import json
import os
import multiprocessing
from pathlib import Path
import shutil
import sqlite3

from aiops_v4.data.discovery import SOURCE_PREFIXES,discover_sources
from aiops_v4.data.position import file_identity,record_at
from aiops_v4.data.resources import ResourcePolicy
from aiops_v4.data.cache import atomic_json,layer_signature,seal_layer,verify_layer,object_hash,recover_layer
from aiops_v4.storage import db_dump,load_json
from .aggregate import iter_windows
from .netflow import NetflowAccumulator
from .spool import parse_file,merge_flow,aggregate_series
from aiops_v4.data.workers import initialize,bounded_results
from .store import create_store,StoreWriter
from .time import time_zone


def build_feature_store(root,batch,output_dir,*,naive_timezone,window_seconds=60,expected_step_seconds=None,
                        counter_max_gap_seconds=180,max_rows_per_file=None,netflow_streaming=True,workers=4,
                        memory_bytes=4*1024**3,min_free_bytes=12*1024**3,max_workspace_bytes=100*1024**3,
                        checkpoint_rows=100000,checkpoint_seconds=30,resume=False,progress=None,workspace_root=None):
    root=Path(root).resolve(strict=True);output=Path(output_dir)
    if output.is_symlink():raise ValueError('unsafe output directory')
    output=output.resolve()
    if output==root or root in output.parents:raise ValueError('output must be outside input root')
    if not isinstance(batch,str) or not batch.strip():raise ValueError('nonempty batch required')
    time_zone(naive_timezone)
    for value in (window_seconds,counter_max_gap_seconds,checkpoint_rows):
        if type(value) is not int or value<1:raise ValueError('positive integer policy required')
    if expected_step_seconds is not None and (type(expected_step_seconds) is not int or expected_step_seconds<1 or window_seconds%expected_step_seconds):raise ValueError('invalid sampling cadence')
    if max_rows_per_file is not None and (type(max_rows_per_file) is not int or max_rows_per_file<1):raise ValueError('invalid prefix limit')
    if type(resume) is not bool or type(netflow_streaming) is not bool or not isinstance(checkpoint_seconds,(int,float)) or checkpoint_seconds<=0:raise ValueError('invalid scan policy')
    workspace_root=Path(workspace_root).resolve() if workspace_root is not None else output
    if workspace_root!=output and workspace_root not in output.parents:raise ValueError('workspace must contain feature output')
    policy=ResourcePolicy(workspace_root,min_free_bytes,max_workspace_bytes,memory_bytes,workers)
    files=discover_sources(root)
    if not files:raise ValueError('no recognized CSV inputs')
    settings=dict(naive_timezone=naive_timezone,window_seconds=window_seconds,expected_step_seconds=expected_step_seconds,
        counter_max_gap_seconds=counter_max_gap_seconds,max_rows_per_file=max_rows_per_file,netflow_streaming=netflow_streaming,
        workers=workers,memory_bytes=memory_bytes,min_free_bytes=min_free_bytes,max_workspace_bytes=max_workspace_bytes,
        checkpoint_rows=checkpoint_rows,checkpoint_seconds=checkpoint_seconds,workspace_root=str(workspace_root))
    producer_config=dict(batch=batch,raw_root=str(root),naive_timezone=naive_timezone,features={k:settings[k] for k in ('window_seconds','expected_step_seconds','counter_max_gap_seconds','netflow_streaming')},profile=dict(max_rows_per_file=max_rows_per_file))
    snapshot=[dict(path=f.relative_path,source=f.source,identity=file_identity(f.path)) for f in files]
    signature=layer_signature('features',producer_config,snapshot)
    if output.exists():
        if not resume:raise FileExistsError('feature output already exists')
        if (output/'seal.json').exists():
            summary=verify_layer(output,verify_raw=True)
            if summary['layer_signature']!=signature:raise ValueError('feature producer/config/input changed')
            return summary
        saved=json.loads((output/'.build.json').read_text())
        if saved['signature']!=signature:raise ValueError('feature producer/config/input changed since checkpoint')
        if (output/'summary.json').exists():
            summary=recover_layer(output,signature,verify_raw=True)
            if (output/'.work').exists():shutil.rmtree(output/'.work')
            (output/'.build.json').unlink();return summary
    else:
        probe=output.parent
        while not probe.exists():probe=probe.parent
        if shutil.disk_usage(probe).free<min_free_bytes:raise RuntimeError('disk reserve exhausted')
        output.mkdir(parents=True)
        atomic_json(output/'.build.json',dict(signature=signature))
    work=output/'.work';work.mkdir(exist_ok=True)
    policy.check()
    tasks=[(f,batch,str(work/f'file_{i}.sqlite'),settings) for i,f in enumerate(files)]
    manifests={}
    # Schedule large files first; merge and IDs still follow discovery order.
    scheduled=sorted(tasks,key=lambda t:-t[0].path.stat().st_size)
    if workers==1:
        for task in scheduled:manifests[task[0].relative_path]=parse_file(task,progress)
    else:
        context=multiprocessing.get_context('spawn');stop=context.Event()
        pool=ProcessPoolExecutor(max_workers=workers,mp_context=context,initializer=initialize,initargs=(stop,))
        try:
            for task,manifest in bounded_results(pool,parse_file,scheduled,workers):
                manifests[task[0].relative_path]=manifest;policy.check()
                if progress:progress(dict(event='file_done',path=task[0].relative_path,rows=manifest['rows_scanned']))
        except BaseException:stop.set();raise
        finally:pool.shutdown(wait=True,cancel_futures=True)
    manifests=[manifests[f.relative_path] for f in files]
    if snapshot!=[dict(path=f.relative_path,source=f.source,identity=file_identity(f.path)) for f in files]:raise ValueError('raw inputs changed during scan')
    merged_path=work/'merged.sqlite'
    # A merged spool is rebuilt only if it was interrupted before its commit marker.
    merge_done=work/'merge.json'
    if not merge_done.exists():
        if merged_path.exists():merged_path.unlink()
        merged=sqlite3.connect(merged_path);merged.execute('PRAGMA cache_size=-8192');merged.execute('PRAGMA temp_store=FILE')
        merged.execute('CREATE TABLE observations(series TEXT,time TEXT,file INTEGER,idx INTEGER,payload BLOB)')
        flows=NetflowAccumulator(merged,window_seconds,expected_step_seconds,capacity=512) if netflow_streaming else None
        try:
            for file_index,task in enumerate(tasks):
                inp=sqlite3.connect(Path(task[2]).as_uri()+'?mode=ro',uri=True)
                try:
                    with policy.guard_sql(merged):
                        merged.executemany('INSERT INTO observations VALUES (?,?,?,?,?)',((s,t,file_index,i,p) for s,t,i,p in inp.execute('SELECT series,time,idx,payload FROM observations')))
                    if flows is not None and task[0].source=='netflow':
                        for series,start,payload in inp.execute('SELECT series,start,payload FROM flow_windows ORDER BY series,start'):
                            saved=merged.execute('SELECT payload FROM flow_windows WHERE series=? AND start=?',(series,start)).fetchone()
                            value=merge_flow(load_json(saved[0]),load_json(payload)) if saved else load_json(payload)
                            merged.execute('INSERT OR REPLACE INTO flow_windows VALUES (?,?,?)',(series,start,db_dump(value)))
                        merged.executemany('INSERT OR IGNORE INTO flow_times VALUES (?,?,?)',inp.execute('SELECT series,start,time FROM flow_times'))
                finally:inp.close()
                merged.commit();policy.check()
            with policy.guard_sql(merged):merged.execute('CREATE INDEX observations_order ON observations(series,time,file,idx)')
            merged.commit()
            atomic_json(merge_done,dict(signature=signature))
        finally:merged.close()
    merged=sqlite3.connect(merged_path.as_uri()+'?mode=ro',uri=True)
    target=output/'.features.sqlite'
    # Completed native groups remain committed across interruption.
    db=create_store(target) if not target.exists() else sqlite3.connect(target)
    writer=StoreWriter.__new__(StoreWriter)
    if not db.execute('SELECT count(*) FROM files').fetchone()[0]:writer=StoreWriter(db,batch,manifests);db.commit()
    else:
        writer.db=db;writer.batch=batch
        writer.file_ids={p:i for i,p in db.execute('SELECT id,path FROM files')}
        writer.groups={s:i for i,s in db.execute('SELECT id,series FROM groups')}
        key=lambda v:json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        writer.definitions={key(load_json(v)):i for i,v in db.execute('SELECT id,payload FROM definitions')}
        writer.flags={key(load_json(v)):i for i,v in db.execute('SELECT id,payload FROM flags')}
        writer.extras={key(load_json(v)):i for i,v in db.execute('SELECT id,payload FROM extras')}
    completed={r[0] for r in db.execute('SELECT grp FROM progress')}
    by_file={f['path']:f for f in manifests}
    def canonicalize(window):
        anchors={};refs=[]
        for ref in window['references']:
            manifest=by_file[ref['source_file']]
            if ref['record_id'].startswith('position:'):
                # Recover the immutable old ID only for the selected finite references.
                from aiops_v4.data.position import record_id
                cells=ref['_cells']
                raw={n:cells[i] if i<len(cells) else None for i,n in enumerate(manifest['columns'])}
                ref=dict(ref,record_id=record_id(batch,manifest['path'],ref['record_index'],raw,tuple(cells[len(manifest['columns']):])))
            ref={k:v for k,v in ref.items() if k!='_cells'}
            anchors[ref['record_id']]=ref
            refs.append({k:ref[k] for k in ('record_id','source_file','record_index','line_start','line_end')})
        window['references']=refs
        return window,anchors
    def commit_group(series):
        db.execute('INSERT INTO progress VALUES (?)',(series,));db.commit();policy.check()
        if progress:progress(dict(event='group_complete',group=series,window_count=db.execute('SELECT count(*) FROM windows').fetchone()[0]))
    try:
        groups=[r[0] for r in merged.execute('SELECT DISTINCT series FROM observations ORDER BY series')]
        for series in completed:
            if progress:progress(dict(event='group_reused',group=series))
        tasks_aggregate=[(str(merged_path),batch,series,settings,str(work/f'group_{index}.sqlite')) for index,series in enumerate(groups) if series not in completed]
        context=multiprocessing.get_context('spawn');stop=context.Event()
        pool=ProcessPoolExecutor(max_workers=workers,mp_context=context,initializer=initialize,initargs=(stop,)) if workers>1 else None
        try:
            results=bounded_results(pool,aggregate_series,tasks_aggregate,workers) if pool else ((t,aggregate_series(t)) for t in tasks_aggregate)
            for task,result in results:
                series=task[2];inp=sqlite3.connect(Path(result['path']).as_uri()+'?mode=ro',uri=True)
                try:
                    for number,(payload,) in enumerate(inp.execute('SELECT payload FROM windows ORDER BY position'),1):
                        value,anchors=canonicalize(load_json(payload));writer.add(value,anchors)
                        if number%1000==0:policy.check()
                    db.execute('INSERT INTO progress VALUES (?)',(series,));db.commit();policy.check()
                    if progress:progress(dict(event='group_complete',group=series,aggregation_worker_pid=result['worker_pid']))
                finally:inp.close()
                Path(result['path']).unlink()
        except BaseException:
            stop.set();raise
        finally:
            if pool:pool.shutdown(wait=True,cancel_futures=True)
        if netflow_streaming:
            flow=NetflowAccumulator.__new__(NetflowAccumulator);flow.db=merged;flow.width=window_seconds;flow.step=expected_step_seconds;flow.cache=OrderedDict()
            # Inherited reader writes cache and commits; reopen the completed spool writable.
            merged.close();merged=sqlite3.connect(merged_path);flow.db=merged
            current=None
            for number,window in enumerate(flow.windows(),1):
                series=json.dumps([batch,window['source'],window['identity']['entity_id'],window['dimensions']],ensure_ascii=False,sort_keys=True,separators=(',',':'))
                if current!=series:
                    if current is not None and current not in completed:commit_group(current)
                    current=series
                if series in completed:continue
                value,anchors=canonicalize(window);writer.add(value,anchors)
                if number%1000==0:policy.check()
            if current is not None and current not in completed:commit_group(current)
        db.execute('CREATE INDEX IF NOT EXISTS window_group_time ON windows(grp,start,id)');db.commit()
        count=db.execute('SELECT count(*) FROM windows').fetchone()[0]
        sources={s:dict(files=0,rows=0,windows=0) for s in SOURCE_PREFIXES}
        for f in manifests:sources[f['source']]['files']+=1;sources[f['source']]['rows']+=f['rows_scanned']
        for s,n in db.execute('SELECT g.source,count(*) FROM windows w JOIN groups g ON g.id=w.grp GROUP BY g.source'):sources[s]['windows']=n
        accepted=sum(f['essential_profile']['accepted'] for f in manifests);rejected=sum(f['essential_profile']['rejected'] for f in manifests)
        summary=dict(schema_version=1,storage_format='canonical_features_v1',batch=batch,input_root=str(root),output_dir=str(output),
            mode='full' if max_rows_per_file is None else 'prefix_sample',max_rows_per_file=max_rows_per_file,
            complete=all(f['complete'] for f in manifests),rows_scanned=sum(f['rows_scanned'] for f in manifests),rows_accepted=accepted,rows_rejected=rejected,
            window_count=count,files=manifests,sources=sources,profile_policy='essential',csv_parse_passes=1,naive_timezone=naive_timezone,
            timezone_officially_confirmed=False,window_seconds=window_seconds,expected_step_seconds=expected_step_seconds,
            counter_max_gap_seconds=counter_max_gap_seconds,netflow_streaming=netflow_streaming,
            resource_policy=dict(workers=workers,memory_bytes=memory_bytes,min_free_bytes=min_free_bytes,max_workspace_bytes=max_workspace_bytes),peak_workspace_bytes=policy.peak_bytes)
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('summary',?)",(db_dump(summary),));db.commit()
        with policy.guard_sql(db):db.execute('VACUUM')
        db.close();db=None
        if snapshot!=[dict(path=f.relative_path,source=f.source,identity=file_identity(f.path)) for f in files]:raise ValueError('raw inputs changed during aggregation')
        if layer_signature('features',producer_config,snapshot)!=signature:raise ValueError('feature producer changed during build')
        target.rename(output/'features.sqlite')
        summary=seal_layer(output,summary,signature=signature)
        shutil.rmtree(work);(output/'.build.json').unlink()
        return summary
    finally:
        if db is not None:db.close()
        merged.close()

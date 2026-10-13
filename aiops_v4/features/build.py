"""One CSV parse per input, private file spools, canonical minute-store publication."""
from collections import Counter,OrderedDict
from contextlib import closing
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
from .store import create_store,StoreWriter,configure_build_connection,finalize_build_connection
from .source_stream import iter_netflow_windows
from .spool_codec import series_keys
from .time import time_zone
from .write_codec import canonicalize_window


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
            if (output/'.work').exists():shutil.rmtree(output/'.work')
            if (output/'.build.json').exists():(output/'.build.json').unlink()
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
            for task,manifest in bounded_results(pool,parse_file,scheduled,workers,ordered=False):
                manifests[task[0].relative_path]=manifest;policy.check()
                if progress:progress(dict(event='file_done',path=task[0].relative_path,rows=manifest['rows_scanned']))
        except BaseException:stop.set();raise
        finally:pool.shutdown(wait=True,cancel_futures=True)
    manifests=[manifests[f.relative_path] for f in files]
    if snapshot!=[dict(path=f.relative_path,source=f.source,identity=file_identity(f.path)) for f in files]:raise ValueError('raw inputs changed during scan')
    # Each parser owns an indexed private spool. Keep one copy and merge only
    # the current series across files in discovery order inside the worker.
    native_sources={};flow_sources=[]
    for file_index,task in enumerate(tasks):
        path=Path(task[2]);source=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            for series in series_keys(source):
                native_sources.setdefault(series,[]).append((file_index,str(path)))
            if netflow_streaming and task[0].source=='netflow':
                flow_sources.append((file_index,str(path)))
        finally:source.close()
    target=output/'.features.sqlite'
    # Completed native groups remain committed across interruption.
    db=create_store(target,memory_bytes=memory_bytes,workers=workers) if not target.exists() else sqlite3.connect(target)
    configure_build_connection(db,memory_bytes=memory_bytes,workers=workers)
    if not db.execute('SELECT count(*) FROM files').fetchone()[0]:
        writer=StoreWriter(db,batch,manifests);db.commit()
    else:writer=StoreWriter.resume(db,batch)
    completed={r[0] for r in db.execute('SELECT grp FROM progress')}
    by_file={f['path']:f for f in manifests}
    def canonicalize(window):
        return canonicalize_window(window,by_file,batch)
    def commit_group(series):
        db.execute('INSERT INTO progress VALUES (?)',(series,));db.commit();policy.check()
        if progress:progress(dict(event='group_complete',group=series,window_count=db.execute('SELECT count(*) FROM windows').fetchone()[0]))
    try:
        groups=sorted(native_sources)
        for series in completed:
            if progress:progress(dict(event='group_reused',group=series))
        tasks_aggregate=[(native_sources[series],batch,series,settings,str(work/f'group_{index}.sqlite')) for index,series in enumerate(groups) if series not in completed]
        context=multiprocessing.get_context('spawn');stop=context.Event()
        pool=ProcessPoolExecutor(max_workers=workers,mp_context=context,initializer=initialize,initargs=(stop,)) if workers>1 else None
        try:
            results=bounded_results(pool,aggregate_series,tasks_aggregate,workers) if pool else ((t,aggregate_series(t)) for t in tasks_aggregate)
            for task,result in results:
                series=task[2];inp=sqlite3.connect(Path(result['path']).as_uri()+'?mode=ro',uri=True)
                try:
                    writer.import_prepared(inp,batch_size=512,check=policy.check)
                    db.execute('INSERT INTO progress VALUES (?)',(series,));db.commit();policy.check()
                    if progress:progress(dict(event='group_complete',group=series,aggregation_worker_pid=result['worker_pid']))
                finally:inp.close()
                Path(result['path']).unlink()
        except BaseException:
            stop.set();raise
        finally:
            if pool:pool.shutdown(wait=True,cancel_futures=True)
        if netflow_streaming:
            def flow_series(window):
                return json.dumps([batch,window['source'],window['identity']['entity_id'],window['dimensions']],ensure_ascii=False,sort_keys=True,separators=(',',':'))
            with closing(iter_netflow_windows(flow_sources,window_seconds,expected_step_seconds,completed)) as flow_windows:
                for series,windows in groupby(flow_windows,key=flow_series):
                    def values():
                        for number,window in enumerate(windows,1):
                            if number%1000==0:policy.check()
                            yield canonicalize(window)
                    writer.add_many(values(),batch_size=512)
                    commit_group(series)
        db.execute('CREATE INDEX IF NOT EXISTS window_group_time ON windows(grp,start,id)');db.commit()
        count=db.execute('SELECT count(*) FROM windows').fetchone()[0]
        sources={s:dict(files=0,rows=0,windows=0) for s in SOURCE_PREFIXES}
        for f in manifests:sources[f['source']]['files']+=1;sources[f['source']]['rows']+=f['rows_scanned']
        for s,n in db.execute('SELECT g.source,count(*) FROM windows w JOIN groups g ON g.id=w.grp GROUP BY g.source'):sources[s]['windows']=n
        accepted=sum(f['essential_profile']['accepted'] for f in manifests);rejected=sum(f['essential_profile']['rejected'] for f in manifests)
        summary=dict(schema_version=1,storage_format='canonical_features_v1',batch=batch,input_root=str(root),output_dir=str(output),
            mode='full' if max_rows_per_file is None else 'prefix_sample',max_rows_per_file=max_rows_per_file,
            complete=all(f['complete'] for f in manifests),rows_scanned=sum(f['rows_scanned'] for f in manifests),rows_accepted=accepted,rows_rejected=rejected,
            window_count=count,files=manifests,sources=sources,profile_policy='essential',csv_parse_passes=1,intermediate_policy='private_spools_direct_merge',naive_timezone=naive_timezone,
            timezone_officially_confirmed=False,window_seconds=window_seconds,expected_step_seconds=expected_step_seconds,
            counter_max_gap_seconds=counter_max_gap_seconds,netflow_streaming=netflow_streaming,
            resource_policy=dict(workers=workers,memory_bytes=memory_bytes,min_free_bytes=min_free_bytes,max_workspace_bytes=max_workspace_bytes),peak_workspace_bytes=policy.peak_bytes)
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('summary',?)",(db_dump(summary),));db.commit()
        finalize_build_connection(db)
        db.close();db=None
        if snapshot!=[dict(path=f.relative_path,source=f.source,identity=file_identity(f.path)) for f in files]:raise ValueError('raw inputs changed during aggregation')
        if layer_signature('features',producer_config,snapshot)!=signature:raise ValueError('feature producer changed during build')
        target.rename(output/'features.sqlite')
        summary=seal_layer(output,summary,signature=signature)
        shutil.rmtree(work);(output/'.build.json').unlink()
        return summary
    finally:
        if db is not None:db.close()

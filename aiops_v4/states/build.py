"""Unchanged per-group state algorithms, persisted as deltas instead of matrices."""
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import json
import math
import os
import multiprocessing
from aiops_v4.data.workers import initialize,bounded_results,check_cancel
from pathlib import Path
import shutil
import sqlite3

from aiops_v4.data.cache import atomic_json,layer_signature,seal_layer,verify_layer,sha256,recover_layer
from aiops_v4.data.reader import utc_time
from aiops_v4.data.resources import ResourcePolicy
from aiops_v4.features.store import FeatureStore,micros,configure_build_connection,finalize_build_connection
from aiops_v4.storage import db_dump,load_json
from .store import GRAIN
from .matrix import window_vector
from .reference import fit_reference,normalize
from .cluster import fit_clusters
from .signals import assess
from .selection import select_candidates
from .events import EventBuilder
from .codec import DeltaWriter,pack_state_delta,remap_delta,STATUS_CODES
from .vector_batch import VectorReplay
from .blocks import BlockReader,batches,pack_block,MAX_ROWS,TARGET_BYTES

VECTOR_STATISTICS=('last','min','max','mean','delta_sum','rate_mean','change_count','counter_decrease_count','counter_gap_count','sum','invalid_input_count','conflicting_time_count')

_FEATURES=None
_POLICY=None


def initialize_worker(path,policy,stop):
    global _FEATURES,_POLICY
    initialize(stop);policy.worker_limit();_POLICY=policy;_FEATURES=FeatureStore(path)


def compute_group(task,features=None,policy=None):
    path=Path(task[3])
    active_policy=policy or _POLICY
    budget=min(128*1024*1024,active_policy.memory_bytes//(8*active_policy.workers)) if active_policy and hasattr(active_policy,'memory_bytes') else 128*1024*1024
    with VectorReplay(path.with_suffix(".vectors"),memory_limit=budget) as replay:
        return _compute_group(task,features,policy,replay)


def _compute_group(task,features,policy,replay):
    group,group_id,settings,path=task;features=features or _FEATURES;policy=policy or _POLICY;path=Path(path)
    if path.exists():path.unlink()  # This group's unsealed private spool only.
    db=sqlite3.connect(path);db.executescript('BEGIN; PRAGMA user_version=3; CREATE TABLE states(id BLOB UNIQUE NOT NULL,start INTEGER,end INTEGER,candidate INTEGER,status INTEGER,quality INTEGER,suppressed INTEGER,payload BLOB,block INTEGER NOT NULL,position INTEGER NOT NULL,seq INTEGER PRIMARY KEY); CREATE TABLE state_blocks(id INTEGER PRIMARY KEY,count INTEGER NOT NULL,payload BLOB NOT NULL); CREATE TABLE events(id TEXT PRIMARY KEY,start INTEGER,end INTEGER,payload BLOB); CREATE TABLE metadata(key TEXT PRIMARY KEY,payload BLOB);')
    try:
        return _score_group(group,settings,path,features,policy,replay,db)
    finally:db.close()


def _score_group(group,settings,path,features,policy,replay,db):
    codec=DeltaWriter(db)
    def vectors():
        for index,window in enumerate(features.windows(group,page_size=64,include_references=True,statistics_fields=VECTOR_STATISTICS),1):
            if index%256==0:
                check_cancel()
                if policy:policy.check()
            yield window_vector(window,features.summary['batch'])
    def reference_rows():
        for row in replay.capture(vectors()):
            if settings['reference_start'] is not None and not (settings['reference_start']<=row['start_time'] and row['end_time']<=settings['reference_end']):continue
            yield row
    reference=fit_reference(reference_rows(),settings['min_reference']);points=[]
    for row in reference.pop('_sample_rows'):
        normalized=normalize(row,reference)
        if normalized['coverage']>=settings['minimum_overlap']:points.append(normalized['values'])
    clustering=fit_clusters(points,k=settings['clusters'],minimum_overlap=settings['minimum_overlap'])
    model=dict(group_id=group,reference=reference,clustering=clustering)
    thresholds={k:settings[k] for k in ('stat_threshold','cluster_threshold','rare_fraction','minimum_overlap','rules_enabled')}
    def assessed():
        previous=None
        for index,row in enumerate(replay.rows(),1):
            if index%256==0:
                check_cancel()
                if policy:policy.check()
            if previous is not None and row['start_time']<previous[0]['end_time']:raise ValueError('overlapping windows within native series')
            row['in_reference']=settings['reference_start'] is None or (settings['reference_start']<=row['start_time'] and row['end_time']<=settings['reference_end'])
            state=assess(row,reference,clustering,settings['mode'],thresholds,previous);previous=row,state
            yield row,state
    selected=assessed()
    if settings['candidate_policy']=='conservative':selected=select_candidates(selected,reference,threshold=settings['stat_threshold'],minimum_windows=settings['candidate_min_windows'],rules_enabled=settings['rules_enabled'])
    counts=Counter(groups=1,ready_models=int(reference['status']=='ready'));statuses=Counter();triggers=Counter();sources=Counter();builder=EventBuilder()
    def emit(event):
        db.execute('INSERT INTO events VALUES (?,?,?,?)',(event['event_id'],micros(event['start_time']),micros(event['end_time']),db_dump(event)));counts['candidate_events']+=1
    pending=[];pending_bytes=0
    def flush():
        nonlocal pending,pending_bytes
        if not pending:return
        block=db.execute('INSERT INTO state_blocks(count,payload) VALUES (?,?)',(len(pending),pack_block(row[-1] for row in pending))).lastrowid
        db.executemany('INSERT INTO states(id,start,end,candidate,status,quality,suppressed,payload,block,position) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (tuple(row[:-1])+(None,block,position) for position,row in enumerate(pending)))
        pending=[];pending_bytes=0
    for row,state in selected:
        delta={k:v for k,v in state.items() if k not in GRAIN}
        suppressed=state.get('raw_candidate',False) and not state['candidate']
        quality=bool(state['quality_flags'] or state['quality_signals'] or state['scoring_status']!='scored')
        payload=pack_state_delta(delta,row,reference['status'],codec.intern,compress=False)
        if pending and (len(pending)>=MAX_ROWS or pending_bytes+len(payload)>TARGET_BYTES):flush()
        pending.append((bytes.fromhex(state['vector_id']),micros(state['start_time']),micros(state['end_time']),state['candidate'],STATUS_CODES[state['scoring_status']],quality,suppressed,payload));pending_bytes+=len(payload)
        statuses[state['scoring_status']]+=1;sources[row['source']]+=1
        counts['states']+=1;counts['candidate_windows']+=state['candidate'];counts['raw_candidate_windows']+=state.get('raw_candidate',state['candidate'])
        counts['suppressed_candidate_windows']+=suppressed;counts['state_changes']+=state['state_changed']
        counts['quality_signal_windows']+=bool(state['quality_signals']);counts['references_truncated_states']+=row['references_truncated']
        for trigger in state['triggers']:triggers[':'.join(trigger.split(':')[:2])]+=1
        for event in builder.push(row,state):emit(event)
    flush()
    for event in builder.finish():emit(event)
    stats=dict(counts=dict(counts),statuses=dict(statuses),triggers=dict(triggers),sources=dict(sources))
    db.execute("INSERT INTO metadata VALUES ('model',?)",(db_dump(model),));db.execute("INSERT INTO metadata VALUES ('stats',?)",(db_dump(stats),));db.commit()
    return str(path)


def build_state_store(features_dir,batch,output_dir,*,reference_start=None,reference_end=None,min_reference=12,clusters=3,mode='hybrid',
                      stat_threshold=6.,cluster_threshold=3.,rare_fraction=.1,minimum_overlap=.5,rules_enabled=True,candidate_policy='legacy',candidate_min_windows=2,
                      workers=4,memory_bytes=4*1024**3,min_free_bytes=12*1024**3,max_workspace_bytes=100*1024**3,resume=False,progress=None):
    features_dir=Path(features_dir).resolve(strict=True);output=Path(output_dir).resolve()
    if output.parent!=features_dir.parent or output==features_dir:raise ValueError('state and feature stores must share a dataset root')
    if mode not in {'statistics','cluster','hybrid'} or candidate_policy not in {'legacy','conservative'}:raise ValueError('invalid state policy')
    for value,lo,hi in ((min_reference,2,256),(clusters,1,8),(candidate_min_windows,1,60)):
        if type(value) is not int or not lo<=value<=hi:raise ValueError('invalid state integer setting')
    for value in (stat_threshold,cluster_threshold,minimum_overlap):
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise ValueError('invalid state numeric setting')
    if minimum_overlap>1 or type(rare_fraction) not in (int,float) or not math.isfinite(rare_fraction) or not 0<=rare_fraction<=1 or type(rules_enabled) is not bool:raise ValueError('invalid state setting')
    if (reference_start is None)!=(reference_end is None):raise ValueError('reference interval pair required')
    if reference_start is not None:
        reference_start=utc_time(reference_start,require_timezone=True)[0];reference_end=utc_time(reference_end,require_timezone=True)[0]
        if reference_start>=reference_end:raise ValueError('reference interval must increase')
    settings=dict(reference_start=reference_start,reference_end=reference_end,min_reference=min_reference,clusters=clusters,mode=mode,
        stat_threshold=stat_threshold,cluster_threshold=cluster_threshold,rare_fraction=rare_fraction,minimum_overlap=minimum_overlap,
        rules_enabled=rules_enabled,candidate_policy=candidate_policy,candidate_min_windows=candidate_min_windows)
    feature_summary=verify_layer(features_dir)
    if feature_summary['batch']!=batch:raise ValueError('feature batch mismatch')
    deps={f'../{features_dir.name}/{n}':sha256(features_dir/n) for n in ('features.sqlite','summary.json','seal.json')}
    signature=layer_signature('states',dict(batch=batch,states=settings),list(deps.values()))
    if output.exists():
        if not resume:raise FileExistsError('state output exists')
        if (output/'seal.json').exists():
            summary=verify_layer(output)
            if summary['layer_signature']!=signature:raise ValueError('state producer/config/input changed')
            if (output/'.work').exists():shutil.rmtree(output/'.work')
            if (output/'.build.json').exists():(output/'.build.json').unlink()
            return summary
        if json.loads((output/'.build.json').read_text())['signature']!=signature:raise ValueError('state checkpoint changed')
        if (output/'summary.json').exists():
            summary=recover_layer(output,signature)
            if (output/'.work').exists():shutil.rmtree(output/'.work')
            (output/'.build.json').unlink();return summary
    else:
        output.mkdir();atomic_json(output/'.build.json',dict(signature=signature))
    policy=ResourcePolicy(output.parent,min_free_bytes,max_workspace_bytes,memory_bytes,workers);policy.check()
    work=output/'.work';work.mkdir(exist_ok=True);target=output/'.states.sqlite'
    existing=target.exists();db=sqlite3.connect(target)
    if existing and db.execute('PRAGMA user_version').fetchone()[0]!=3:
        db.close();raise ValueError('incompatible state checkpoint format; rebuild in a new directory')
    db.executescript('''PRAGMA cache_size=-8192;PRAGMA user_version=3;
      CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,payload BLOB);
      CREATE TABLE IF NOT EXISTS models(grp TEXT PRIMARY KEY,payload BLOB,native_grp INTEGER UNIQUE,reference_status TEXT);
      CREATE TABLE IF NOT EXISTS deltas(id BLOB UNIQUE NOT NULL,grp INTEGER,grp_order INTEGER,start INTEGER,end INTEGER,candidate INTEGER,status INTEGER,quality INTEGER,suppressed INTEGER,payload BLOB,block INTEGER NOT NULL,position INTEGER NOT NULL,seq INTEGER PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS state_blocks(id INTEGER PRIMARY KEY,count INTEGER NOT NULL,payload BLOB NOT NULL);
      CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,entity TEXT,grp TEXT,grp_order INTEGER,start INTEGER,end INTEGER,payload BLOB);
      CREATE TABLE IF NOT EXISTS event_windows(id BLOB PRIMARY KEY,event TEXT) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS progress(grp TEXT PRIMARY KEY,payload BLOB);
    ''')
    configure_build_connection(db,memory_bytes=memory_bytes,workers=workers)
    completed={r[0] for r in db.execute('SELECT grp FROM progress')}
    codec=DeltaWriter(db)
    totals=Counter();statuses=Counter();triggers=Counter();sources=Counter()
    def add_stats(stats):totals.update(stats['counts']);statuses.update(stats['statuses']);triggers.update(stats['triggers']);sources.update(stats['sources'])
    for p, in db.execute('SELECT payload FROM progress'):add_stats(load_json(p))
    try:
        # Event membership is queried while groups are merged, so the lookup
        # index must exist before the first event reaches the parent store.
        # Resuming a large earlier checkpoint can itself require a guarded sort.
        with policy.guard_sql(db):db.execute('CREATE INDEX IF NOT EXISTS delta_group_time ON deltas(grp,start,id)')
        with FeatureStore(features_dir/'features.sqlite') as store:
            groups=list(store.db.execute('SELECT state_group,id,entity FROM groups ORDER BY state_group'))
            tasks=[(g,i,settings,str(work/(g+'.sqlite'))) for g,i,e in groups if g not in completed]
            for g in completed:
                if progress:progress(dict(event='group_reused',group=g))
            context=multiprocessing.get_context('spawn');stop=context.Event()
            pool=ProcessPoolExecutor(max_workers=workers,mp_context=context,initializer=initialize_worker,initargs=(str(store.path),policy,stop)) if workers>1 else None
            try:
                results=bounded_results(pool,compute_group,tasks,workers) if pool else ((t,compute_group(t,store,policy)) for t in tasks)
                entity_by_group={g:e for g,i,e in groups}
                order_by_group={g:index for index,(g,i,e) in enumerate(groups,1)}
                for task,path in results:
                    group,group_id,_,_=task;inp=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)
                    try:
                        model=load_json(inp.execute("SELECT payload FROM metadata WHERE key='model'").fetchone()[0]);stats=load_json(inp.execute("SELECT payload FROM metadata WHERE key='stats'").fetchone()[0])
                        if inp.execute('PRAGMA user_version').fetchone()[0]!=3:raise ValueError('incompatible typed state worker format')
                        worker_strings=dict(inp.execute('SELECT id,value FROM strings'))
                        tokens={i:codec.intern(value) for i,value in worker_strings.items()}
                        db.execute('INSERT INTO models VALUES (?,?,?,?)',(group,db_dump(model),group_id,model['reference']['status']))
                        reader=BlockReader(inp)
                        def transferred():
                            for i,s,e,c,st,q,sp,block,position in inp.execute('SELECT id,start,end,candidate,status,quality,suppressed,block,position FROM states ORDER BY start,id'):
                                payload=remap_delta(reader.get(block,position),tokens,strings=worker_strings,feature_names=model['reference']['features'],compress=False)
                                yield (i,group_id,order_by_group[group],s,e,c,st,q,sp,payload)
                        with policy.guard_sql(db):
                            for page in batches(transferred()):
                                block=db.execute('INSERT INTO state_blocks(count,payload) VALUES (?,?)',(len(page),pack_block(row[-1] for row in page))).lastrowid
                                db.executemany('INSERT INTO deltas(id,grp,grp_order,start,end,candidate,status,quality,suppressed,payload,block,position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                    (tuple(row[:-1])+(None,block,position) for position,row in enumerate(page)))
                        for ident,start,end,payload in inp.execute('SELECT * FROM events ORDER BY start,id'):
                            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)',(ident,entity_by_group[group],group,order_by_group[group],start,end,payload))
                            db.execute('INSERT INTO event_windows SELECT id,? FROM deltas WHERE grp=? AND candidate=1 AND start>=? AND end<=?',(ident,group_id,start,end))
                        db.execute('INSERT INTO progress VALUES (?,?)',(group,db_dump(stats)));db.commit();add_stats(stats)
                    finally:inp.close()
                    Path(path).unlink();policy.check()
                    if progress:progress(dict(event='group_complete',group=group,states=totals['states']))
            except BaseException:
                db.rollback();codec.refresh();stop.set();raise
            finally:
                if pool:pool.shutdown(wait=True,cancel_futures=True)
        if totals['states']!=feature_summary['window_count'] or not totals['states']:raise ValueError('state/window count mismatch or empty')
        # Membership lookup was indexed before merging. Remaining indexes are
        # bulk-built once, guarded by the same resource/cancellation policy.
        with policy.guard_sql(db):
            db.execute('CREATE INDEX IF NOT EXISTS delta_order ON deltas(grp_order,start,id)')
            # Legacy tool joins use hexadecimal IDs; index that expression.
            db.execute('CREATE INDEX IF NOT EXISTS delta_external_id ON deltas(lower(hex(id)))')
            db.execute('CREATE INDEX IF NOT EXISTS event_entity_time ON events(entity,start,end)')
            db.execute('CREATE INDEX IF NOT EXISTS event_window_members ON event_windows(event,id)')
        summary=dict(schema_version=2,storage_format='canonical_states_v3_blocks',batch=batch,windows_dir=str(features_dir),output_dir=str(output),
            feature_dependency=f'../{features_dir.name}/features.sqlite',input_summary_sha256=sha256(features_dir/'summary.json'),input_feature_store_sha256=sha256(features_dir/'features.sqlite'),
            input_scope={k:feature_summary.get(k) for k in ('mode','complete','window_count','rows_scanned','max_rows_per_file','naive_timezone','timezone_officially_confirmed')},
            reference_scope=dict(mode='explicit_interval' if reference_start is not None else 'offline_same_batch',start=reference_start,end=reference_end,known_healthy=False),
            config={k:v for k,v in settings.items() if k not in ('reference_start','reference_end')},
            capacities=dict(features=128,reference_samples=256,cluster_samples=64,categories=16,event_refs=16,event_windows=64),
            **{k:totals[k] for k in ('states','groups','ready_models','candidate_windows','raw_candidate_windows','suppressed_candidate_windows','candidate_events','state_changes','quality_signal_windows','references_truncated_states')},
            scoring_status_counts=dict(sorted(statuses.items())),trigger_counts=dict(sorted(triggers.items())),states_by_source=dict(sorted(sources.items())),diagnoses_confirmed=False)
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('summary',?)",(db_dump(summary),));db.execute("INSERT OR REPLACE INTO metadata VALUES ('feature_dependency',?)",(db_dump(summary['feature_dependency']),));db.commit()
        finalize_build_connection(db)
        db.close();db=None
        if layer_signature('states',dict(batch=batch,states=settings),list(deps.values()))!=signature:raise ValueError('state producer changed during build')
        target.rename(output/'states.sqlite');summary=seal_layer(output,summary,signature=signature,dependencies=deps)
        shutil.rmtree(work);(output/'.build.json').unlink();return summary
    finally:
        if db is not None:db.close()

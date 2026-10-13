"""Small evidence packets over sealed upstream stores; no full state/matrix copy."""
from collections import Counter
import json
from pathlib import Path
import shutil
import sqlite3
from aiops_v4.data.cache import atomic_json,seal_layer,verify_layer,layer_signature,sha256,dependency_path,recover_layer
from aiops_v4.data.resources import ResourcePolicy
from aiops_v4.features.time import observation_time,iso_time
from aiops_v4.storage import db_dump,load_json,jsonl_writer
from aiops_v4.states.store import StateStore
from .association import load_topology
from .index import put_metadata


def attach_linked(db,database):
    relative=load_json(db.execute("SELECT payload FROM metadata WHERE key='state_dependency'").fetchone()[0])
    state=StateStore(dependency_path(Path(database).parent,relative))
    try:
        state.attach_views(db)
        def reference(digest,file,payload):
            row=state.features.decode_anchor(digest,file,payload)
            return db_dump({k:row[k] for k in ('record_id','source_file','record_index','line_start','line_end')})
        db.create_function('v4_ref',3,reference)
        db.create_function('v4_ref_index',3,lambda digest,file,payload:state.features.decode_anchor(digest,file,payload)['record_index'])
        db.executescript('''CREATE TEMP VIEW anchors AS SELECT lower(hex(a.digest)) AS id,f.path AS file,
            v4_ref_index(a.digest,a.file,a.payload) AS idx,v4_ref(a.digest,a.file,a.payload) AS payload
            FROM upstream_features.anchors a JOIN upstream_features.files f ON f.id=a.file;''')
        return state
    except BaseException:state.close();raise


def raw_from_features(features,identifier,raw_root=None):
    from aiops_v4.data.position import record_at
    anchor,manifest=features.anchor(identifier)
    record=record_at(raw_root or features.summary['input_root'],features.summary['batch'],manifest,anchor)
    observed,flags=observation_time(record,features.summary['naive_timezone'])
    result=record.to_dict();result.update(observed_time=iso_time(observed),time_quality_flags=list(flags))
    return result



def _linked_context(db,state,bundle,seconds,cap,state_summary,window_summary,topology,skip_raw):
    """Filter typed native states before reconstructing the bounded examples."""
    from datetime import datetime,timedelta
    from aiops_v4.features.store import micros
    from .index import scoring_provenance
    from .packet import compact_observation,make_packet
    start=iso_time(datetime.fromisoformat(bundle['start_time'].replace('Z','+00:00'))-timedelta(seconds=seconds))
    end=iso_time(datetime.fromisoformat(bundle['end_time'].replace('Z','+00:00'))+timedelta(seconds=seconds))
    # CROSS JOIN keeps the small native-group table outside the indexed time
    # lookup even on stores that do not have a separate entity index.
    native='FROM upstream_features.groups g CROSS JOIN upstream_states.deltas d ON d.grp=g.id'
    overlap='g.entity=? AND d.start<? AND d.end>?'
    interval=[bundle['entity_id'],micros(end),micros(start)]
    support='FROM upstream_states.deltas d JOIN upstream_features.groups g ON g.id=d.grp'
    scored='d.status=0' if state.version>=2 else "d.status='scored'"
    scopes={
        'support':(support,'d.id IN (SELECT w.id FROM upstream_states.event_windows w JOIN members m ON m.event=w.event WHERE m.bundle=?)',[bundle['bundle_id']]),
        'observations_without_current_trigger':(native,overlap+" AND d.candidate=0 AND "+scored+" AND d.suppressed IS NOT 1",interval),
        'quality_context':(native,overlap+' AND (d.quality=1 OR d.suppressed=1)',interval)}
    observations,totals={},{}
    for kind,(tables,condition,args) in scopes.items():
        totals[kind]=db.execute('SELECT count(*) '+tables+' WHERE '+condition,args).fetchone()[0]
        rows=db.execute('SELECT d.id,d.grp,d.payload '+tables+' WHERE '+condition+
                        ' ORDER BY d.start,g.source,g.state_group,d.id LIMIT ?',(*args,cap))
        observations[kind]=[compact_observation(a,b) for a,b in state.reconstruct(rows)]
    availability={source:dict(observed=0,scorable=0,candidates=0) for source in window_summary['sources']}
    for source,observed,scorable,candidates in db.execute("SELECT g.source,count(*),sum("+scored+"),sum(d.candidate) "+native+
                                                        ' WHERE '+overlap+' GROUP BY g.source',interval):
        availability[source]=dict(observed=observed,scorable=scorable,candidates=candidates)
    totals['relations']=db.execute('SELECT count(*) FROM relations WHERE bundle=?',(bundle['bundle_id'],)).fetchone()[0]
    relations=[load_json(p) for p, in db.execute('SELECT payload FROM relations WHERE bundle=? ORDER BY other,relation LIMIT 8',(bundle['bundle_id'],))]
    source_metadata=dict(sources=window_summary['sources'],local_availability=availability,
        scope={k:window_summary[k] for k in ('mode','complete','max_rows_per_file','naive_timezone','timezone_officially_confirmed')},
        context_start=start,context_end=end,raw_materialized=not skip_raw,
        topology_available=topology['available'],topology_provenance=topology['provenance'],
        quality_context_interpretation='quality_or_admission_suppressed; neither_means_known_normal',
        selection='earliest_time_then_source_and_native_group; bounded_examples_not_exhaustive')
    packet=make_packet(bundle,observations['support'],observations['observations_without_current_trigger'],
                       observations['quality_context'],source_metadata,relations,dict(observations_per_kind=cap,totals=totals))
    packet['scoring_provenance']=scoring_provenance(state_summary)
    return packet


def build_linked_evidence(states_dir,batch,output_dir,*,raw_root=None,skip_raw=False,topology=None,context_seconds=120,observations_per_kind=8,
                          min_free_bytes=12*1024**3,max_workspace_bytes=100*1024**3,resume=False,progress=None,compact=True):
    from .pipeline import _associate,_relations
    root=Path(states_dir).resolve(strict=True);output=Path(output_dir).resolve()
    if output.parent!=root.parent or output==root:raise ValueError('linked evidence must share sealed dataset root')
    if type(skip_raw) is not bool or type(context_seconds) is not int or not 0<=context_seconds<=86400 or type(observations_per_kind) is not int or not 1<=observations_per_kind<=32:raise ValueError('invalid evidence policy')
    state_summary=verify_layer(root)
    if state_summary['batch']!=batch:raise ValueError('state batch mismatch')
    topology=load_topology(topology)
    settings=dict(raw_root=str(Path(raw_root).resolve()) if raw_root is not None else None,skip_raw=skip_raw,context_seconds=context_seconds,observations_per_kind=observations_per_kind,topology=topology)
    deps={f'../{root.name}/{n}':sha256(root/n) for n in ('states.sqlite','summary.json','seal.json')}
    signature=layer_signature('evidence',dict(batch=batch,evidence=settings),list(deps.values()))
    if output.exists():
        if not resume:raise FileExistsError('evidence output exists')
        if (output/'seal.json').exists():
            summary=verify_layer(output)
            if summary['layer_signature']!=signature:raise ValueError('evidence producer/config/input changed')
            return summary
        if json.loads((output/'.build.json').read_text())['signature']!=signature:raise ValueError('evidence checkpoint changed')
        if (output/'summary.json').exists():
            summary=recover_layer(output,signature);(output/'.build.json').unlink();return summary
    else:output.mkdir();atomic_json(output/'.build.json',dict(signature=signature))
    policy=ResourcePolicy(output.parent,min_free_bytes,max_workspace_bytes);policy.check()
    target=output/'.evidence.sqlite'
    # Evidence is bounded by admitted candidates; interrupted private rebuild only.
    if target.exists():target.unlink()
    bundles_path=output/'bundles.jsonl.gz'
    if bundles_path.exists():bundles_path.unlink()
    db=sqlite3.connect(target);state=None
    try:
        db.executescript('''PRAGMA cache_size=-8192;PRAGMA user_version=3;
          CREATE TABLE metadata(key TEXT PRIMARY KEY,payload BLOB);
          CREATE TABLE bundles(id TEXT PRIMARY KEY,entity TEXT,start TEXT,end TEXT,payload BLOB);
          CREATE TABLE members(event TEXT PRIMARY KEY,bundle TEXT);
          CREATE TABLE relations(bundle TEXT,other TEXT,relation TEXT,payload BLOB,PRIMARY KEY(bundle,other,relation));
          CREATE TABLE wanted_raw(id TEXT PRIMARY KEY);
          CREATE TABLE raw_records(id TEXT PRIMARY KEY,payload BLOB);
          CREATE INDEX bundle_entity_time ON bundles(entity,start,end);
          CREATE INDEX bundle_members ON members(bundle,event);
        ''')
        put_metadata(db,'state_dependency',f'../{root.name}/states.sqlite')
        state=attach_linked(db,target);window_summary=state.features.summary
        raw=str(Path(raw_root or window_summary['input_root']).resolve())
        for key,value in dict(batch=batch,raw_root=raw,raw_manifest=window_summary['files'],naive_timezone=window_summary['naive_timezone'],
                              state_summary=state_summary,window_summary=window_summary,topology=topology).items():put_metadata(db,key,value)
        counts=Counter(states=state_summary['states'],groups=state_summary['groups'],candidate_windows=state_summary['candidate_windows'],events=state_summary['candidate_events'])
        counts['bundles']=_associate(db,batch);_relations(db,topology)
        with jsonl_writer(output/'bundles.jsonl',True) as stream:
            for number,(identifier,payload) in enumerate(db.execute('SELECT id,payload FROM bundles ORDER BY entity,start,id'),1):
                packet=_linked_context(db,state,load_json(payload),context_seconds,observations_per_kind,state_summary,window_summary,topology,skip_raw)
                stream.write(json.dumps(packet,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
                db.execute('UPDATE bundles SET payload=? WHERE id=?',(db_dump(packet),identifier))
                db.executemany('INSERT OR IGNORE INTO wanted_raw VALUES (?)',((r['record_id'],) for r in packet['references']))
                policy.check()
                if progress:progress(dict(event='bundle_indexed',bundles=number,bundles_total=counts['bundles']))
        if not skip_raw:
            for identifier, in db.execute('SELECT id FROM wanted_raw ORDER BY id'):
                record=raw_from_features(state.features,identifier,raw)
                db.execute('INSERT INTO raw_records VALUES (?,?)',(identifier,db_dump(record)));counts['raw_records_materialized']+=1
                if counts['raw_records_materialized']%100==0:policy.check()
        counts['packet_raw_references']=db.execute('SELECT count(*) FROM wanted_raw').fetchone()[0]
        counts['registered_raw_references']=state.features.db.execute('SELECT count(*) FROM anchors').fetchone()[0]
        counts['relations']=db.execute('SELECT count(*) FROM relations').fetchone()[0];db.commit()
        summary=dict(schema_version=3,storage_format='linked_evidence_v1',batch=batch,states_dir=str(root),windows_dir=str(state.features.path.parent),raw_root=raw,
            output_dir=str(output),state_dependency=f'../{root.name}/states.sqlite',input_state_summary_sha256=sha256(root/'summary.json'),
            input_feature_store_sha256=state_summary['input_feature_store_sha256'],input_scope=state_summary['input_scope'],reference_scope=state_summary['reference_scope'],
            topology=topology,config=dict(context_seconds=context_seconds,observations_per_kind=observations_per_kind,skip_raw=skip_raw),
            capacities=dict(event_ids=32,raw_references=32,relations=8),diagnoses_confirmed=False,raw_scope='selected_packet_anchors_only',
            **{k:counts[k] for k in ('states','groups','candidate_windows','events','bundles','relations','packet_raw_references','registered_raw_references','raw_records_materialized')})
        db.close();db=None;state.close();state=None;target.rename(output/'evidence.sqlite')
        if layer_signature('evidence',dict(batch=batch,evidence=settings),list(deps.values()))!=signature:raise ValueError('evidence producer changed during build')
        from aiops_v4.data.cache import checked_digest
        for relative,digest in deps.items():checked_digest(dependency_path(output,relative),digest)
        result=seal_layer(output,summary,signature=signature,dependencies=deps);(output/'.build.json').unlink();return result
    finally:
        if state:state.close()
        if db:db.close()

"""Reusable data layers; model/prompt settings never invalidate compatible parsing."""
from pathlib import Path
from time import monotonic
from aiops_v4.data.cache import layer_signature,object_hash,sha256,verify_layer
from aiops_v4.data.discovery import discover_sources
from aiops_v4.data.position import file_identity
from aiops_v4.features.build import build_feature_store
from aiops_v4.states.build import build_state_store
from aiops_v4.evidence.linked import build_linked_evidence


def build_data_layers(config,directory,*,resume=False,progress=None,min_free_bytes=None):
    from .lock import exclusive_producer
    directory,_=_output_roots(config,directory)
    with exclusive_producer(directory):
        return _build_data_layers(config,directory,resume=resume,progress=progress,min_free_bytes=min_free_bytes)


def _output_roots(config,directory):
    directory=Path(directory)
    if directory.is_symlink():raise ValueError('data root symlink forbidden')
    directory=directory.resolve();raw=Path(config['raw_root']).resolve(strict=True)
    if directory==raw or raw in directory.parents:raise ValueError('data outputs must be outside raw input')
    return directory,raw


def _build_data_layers(config,directory,*,resume=False,progress=None,min_free_bytes=None):
    directory,raw=_output_roots(config,directory)
    directory.mkdir(parents=True,exist_ok=True)
    storage=config['storage']
    policy=dict(workers=storage['workers'],memory_bytes=int(storage['memory_gib']*1024**3),
        min_free_bytes=int(storage['min_free_gib']*1024**3) if min_free_bytes is None else max(min_free_bytes,int(storage['min_free_gib']*1024**3)),
        max_workspace_bytes=int(storage['max_workspace_gib']*1024**3))
    snapshot=[dict(path=f.relative_path,source=f.source,identity=file_identity(f.path)) for f in discover_sources(raw)]
    feature_sig=layer_signature('features',config,snapshot)
    features=directory/('features_'+object_hash(feature_sig)[:16]);times={};reused=[]
    def step(name,path,operation):
        existing=(path/'seal.json').exists()
        begin=monotonic();value=operation(path.exists())
        times[name]=round(monotonic()-begin,6)
        if existing:reused.append(name)
        if progress:progress(dict(stage=name,event='stage_reused' if existing else 'stage_complete',elapsed_seconds=times[name]))
        return value
    feature_summary=step('features',features,lambda exists:build_feature_store(raw,config['batch'],features,
        naive_timezone=config['naive_timezone'],max_rows_per_file=config['profile']['max_rows_per_file'],
        resume=exists,progress=progress,workspace_root=directory,**config['features'],**policy))
    state_sig=layer_signature('states',config,[sha256(features/n) for n in ('features.sqlite','summary.json','seal.json')])
    states=directory/('states_'+object_hash(state_sig)[:16])
    state_summary=step('states',states,lambda exists:build_state_store(features,config['batch'],states,resume=exists,progress=progress,**config['states'],**policy))
    # The evidence builder includes resolved topology bytes in its signature. Name
    # variants by both the config and actual external topology content.
    topology=config['evidence'].get('topology')
    evidence_sig=layer_signature('evidence',config,[*[sha256(states/n) for n in ('states.sqlite','summary.json','seal.json')],sha256(topology) if topology else None])
    evidence=directory/('evidence_'+object_hash(evidence_sig)[:16])
    evidence_summary=step('evidence',evidence,lambda exists:build_linked_evidence(states,config['batch'],evidence,resume=exists,progress=progress,
        min_free_bytes=policy['min_free_bytes'],max_workspace_bytes=policy['max_workspace_bytes'],**config['evidence']))
    files=feature_summary['files']
    profile=dict(schema_version=1,batch=config['batch'],profile_policy='essential',rows_scanned=feature_summary['rows_scanned'],files_scanned=len(files),errors=[],
        files=[dict(f,size_bytes=f['identity']['size'],mtime_ns=f['identity']['mtime_ns']) for f in files],complete=feature_summary['complete'],
        raw_field_distributions='not_computed',raw_field_cardinality='not_computed',recent_duplicate_diagnostics='not_computed')
    return dict(layers={k:str(v) for k,v in dict(features=features,states=states,evidence=evidence).items()},
                stages=dict(profile=profile,features=feature_summary,states=state_summary,evidence=evidence_summary),
                stage_elapsed_seconds=dict(profile=0.,**times),reused_layers=reused)

"""Canonical data backend for the existing v4 diagnosis workflow."""
from datetime import datetime,timezone
import json
from pathlib import Path
from time import monotonic
from aiops_v4.data.pipeline import build_data_layers
from aiops_v4.data.cache import atomic_json,sha256,verify_layer,object_hash
from aiops_v4.agents.engine import Budget
from aiops_v4.experiments.records import provenance,estimate_cost
from aiops_v4.experiments.roles import run_diagnosis


def run_canonical(config,output_dir,*,backend=None,resume=False,min_free_bytes=0,progress=None):
    from .run import new_output,make_backend,verify_run,_inventory,_raw_snapshot,_external_input
    from aiops_v4.agents.backend import ReplayBackend
    injected=backend is not None
    if not config['agents']['discovery_only']:
        backend=backend or make_backend(config)
        if backend.model!=config['backend']['model'] or backend.metadata()['kind']!=config['backend']['kind']:raise ValueError('backend differs from experiment')
    output=new_output(output_dir,(config['raw_root'],),allow_existing=resume)
    data=Path(config['storage']['data_dir']) if config['storage']['data_dir'] else output/'data_store'
    snapshot=_raw_snapshot(config['raw_root']);metadata=provenance(config)
    metadata['backend_input']=dict(injected=injected,replay_entries_sha256=object_hash(list(backend.entries.values())) if isinstance(backend,ReplayBackend) else None)
    inference_external=getattr(backend,'external_inputs',[]) if backend else []
    signature=dict(config_sha256=object_hash(config),code_sha256=metadata['code_sha256'],raw_snapshot=snapshot,backend_input=metadata['backend_input'],external_inputs=inference_external)
    checkpoint=dict(signature=signature)
    if output.exists():
        if (output/'summary.json').exists():
            if not (output/'seal.json').exists():
                prepared=json.loads((output/'summary.json').read_text())
                saved=json.loads((output/'.run.json').read_text())
                inventory={k:v for k,v in _inventory(output).items() if k not in ('summary.json','seal.json','failure.json')}
                if saved['signature']!=signature or inventory!=prepared['artifact_sha256']:raise ValueError('prepared run changed')
                for path in prepared['data_layers'].values():verify_layer(path)
                verify_layer(prepared['data_layers']['features'],verify_raw=True)
                for source in prepared['external_inputs']:
                    if source!=_external_input(source['path'],source['kind']):raise ValueError('prepared external input changed')
                atomic_json(output/'seal.json',dict(schema_version=1,summary_sha256=sha256(output/'summary.json')))
                if (output/'failure.json').exists():(output/'failure.json').unlink()
            completed=verify_run(output)
            if completed['provenance']['config_sha256']!=signature['config_sha256'] or completed['provenance']['code_sha256']!=signature['code_sha256'] or completed['provenance'].get('backend_input')!=metadata['backend_input'] or completed['raw_snapshot']!=snapshot:raise ValueError('completed run changed')
            verify_layer(completed['data_layers']['features'],verify_raw=True)
            return completed
        checkpoint=json.loads((output/'.run.json').read_text())
        if checkpoint['signature']!=signature:raise ValueError('run checkpoint changed')
        if (output/'failure.json').exists():(output/'failure.json').unlink()
    else:
        output.mkdir(parents=True);atomic_json(output/'config.json',config);atomic_json(output/'.run.json',checkpoint)
    started=monotonic();stage='data'
    try:
        layers=build_data_layers(config,data,resume=resume,progress=progress,min_free_bytes=min_free_bytes)
        stages=layers['stages'];durations=layers['stage_elapsed_seconds'];evidence=Path(layers['layers']['evidence'])/'evidence.sqlite'
        stage='agents'
        if 'agents_sha256' in checkpoint:
            if _inventory(output/'agents')!=checkpoint['agents_sha256'] or checkpoint['evidence_sha256']!=sha256(evidence):raise ValueError('completed agent stage changed')
            agents=json.loads((output/'agents/summary.json').read_text());durations['agents']=checkpoint['agents_elapsed_seconds']
        elif config['agents']['discovery_only']:
            agents_path=output/'agents';agents_path.mkdir(exist_ok=True)
            (agents_path/'predictions.jsonl').write_text('',encoding='utf-8')
            agents=dict(simulated=True,backend_calls=0,usage_reported_calls=0,usage_complete=True,usage={},predictions=0,
                selection_complete=False,diagnosis_complete=False,mode='discovery_only',submission_ready=False)
            atomic_json(agents_path/'summary.json',agents);durations['agents']=0.
        else:
            args=dict(config['agents']);args.pop('discovery_only');args['budget']=Budget(**args['budget']);begin=monotonic()
            agents=run_diagnosis(evidence,config['batch'],output/'agents',backend,progress=progress,**args);durations['agents']=round(monotonic()-begin,6)
        checkpoint.update(agents_sha256=_inventory(output/'agents'),agents_elapsed_seconds=durations['agents'],evidence_sha256=sha256(evidence))
        atomic_json(output/'.run.json',checkpoint)
        stages['agents']=agents
        external=[_external_input(Path(path)/'seal.json','data_layer') for path in layers['layers'].values()]
        if config['evidence']['topology']:external.append(_external_input(config['evidence']['topology'],'topology'))
        external.extend(getattr(backend,'external_inputs',[]) if backend else [])
        windows=stages['features'];partial=not windows['complete'] or windows['mode']!='full' or not agents['selection_complete']
        diagnosis_complete=bool(agents['diagnosis_complete'] and not partial and not config['agents']['discovery_only'])
        usage={k:agents[k] for k in ('simulated','backend_calls','usage_reported_calls','usage_complete','usage')}
        usage['token_pair_reported_calls']=0
        trace=output/'agents/roles.jsonl'
        if trace.exists():
            for line in trace.read_text().splitlines():usage['token_pair_reported_calls']+=sum({'prompt_tokens','completion_tokens'}<=t['usage'].keys() for t in json.loads(line)['trace'])
        if snapshot!=_raw_snapshot(config['raw_root']):raise ValueError('raw changed during run')
        if provenance(config)['code_sha256']!=metadata['code_sha256']:raise ValueError('code changed during run')
        summary=dict(schema_version=1,integrity_version=2,external_inputs=external,status='completed',batch=config['batch'],created_at=datetime.now(timezone.utc).isoformat(),
            raw_snapshot=snapshot,resumed_stages=layers['reused_layers'],runtime_policy=dict(min_free_bytes=min_free_bytes),
            output_dir=str(output),raw_root=config['raw_root'],provenance=metadata,input_fingerprint=object_hash(windows['files']),
            input_scope={k:windows[k] for k in ('mode','complete','max_rows_per_file','rows_scanned','naive_timezone')},
            partial=partial,diagnosis_complete=diagnosis_complete,valid_for_model_quality=not agents['simulated'] and not config['agents']['discovery_only'],
            submission_ready=not agents['simulated'] and diagnosis_complete and not agents.get('experimental',False),experimental=agents.get('experimental',False),
            discovery_only=config['agents']['discovery_only'],predictions=agents['predictions'],stages=stages,
            stage_elapsed_seconds=durations,elapsed_seconds=round(monotonic()-started,6),data_layers=layers['layers'],**usage)
        summary['cost']=estimate_cost(summary,config['pricing'])
        atomic_json(output/'experiment.json',{k:v for k,v in summary.items() if k!='stages'})
        summary['artifact_sha256']=_inventory(output)
        atomic_json(output/'summary.json',summary)
        atomic_json(output/'seal.json',dict(schema_version=1,summary_sha256=sha256(output/'summary.json')))
        return summary
    except (Exception,KeyboardInterrupt) as error:
        atomic_json(output/'failure.json',dict(schema_version=1,status='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',batch=config['batch'],stage=stage,error_type=type(error).__name__))
        raise

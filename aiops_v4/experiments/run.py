"""Label-free raw-to-prediction run; publish success only after all stage checks."""
from dataclasses import asdict
from datetime import datetime,timezone
import os
import hashlib
from pathlib import Path
from time import monotonic
from aiops_v4.agents.backend import HTTPBackend,ReplayBackend
from aiops_v4.agents.engine import Budget
from aiops_v4.agents.jsonio import dumps,loads
from aiops_v4.agents.pipeline import sha256
from aiops_v4.data.discovery import discover_sources
from aiops_v4.data.profile import profile_dataset
from aiops_v4.data.cli import _publish_report
from aiops_v4.features.pipeline import build_features
from aiops_v4.states.pipeline import discover_states
from aiops_v4.evidence.pipeline import build_evidence
from aiops_v4.evidence.association import load_topology
from .config import validate_config
from .knowledge import knowledge_cards
from .records import provenance,hash_object,estimate_cost
from .roles import run_diagnosis


def new_output(path,protected=()):
    path=Path(path)
    if path.exists() or path.is_symlink():raise FileExistsError('output already exists')
    path=path.resolve()
    roots=[Path(__file__).resolve().parents[2]/'data',*[Path(p).resolve() for p in protected]]
    if any(path==r or r in path.parents for r in roots):raise ValueError('output must be outside inputs/data')
    return path


def make_backend(config):
    b=config['backend']
    if b['kind']=='replay':
        entries=[]
        with Path(b['replay_file']).open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():entries.append(loads(line))
        return ReplayBackend(b['model'],entries,capture_requests=False)
    key=os.environ.get(b['api_key_env'])
    if not key:raise ValueError('configured API key environment is missing')
    return HTTPBackend(b['model'],b['base_url'],key,b['timeout'],b['max_response_bytes'],b['max_completion_tokens'])


def _raw_snapshot(root):
    return [dict(path=f.relative_path,source=f.source,size=f.path.stat().st_size,mtime_ns=f.path.stat().st_mtime_ns)
            for f in discover_sources(root)]


def _write(path,value):
    with path.open('x',encoding='utf-8') as stream:stream.write(dumps(value)+'\n')


def verify_run(run_dir):
    root=Path(run_dir).resolve(strict=True)
    try:
        summary_path=root/'summary.json';seal=loads((root/'seal.json').read_text(encoding='utf-8'))
        if (root/'failure.json').exists() or sha256(summary_path)!=seal['summary_sha256']:raise ValueError('invalid run seal')
        summary=loads(summary_path.read_text(encoding='utf-8'))
        if summary['schema_version']!=1 or summary['status']!='completed':raise ValueError('run incomplete')
        expected=set(summary['artifact_sha256'])
        actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}-{'summary.json','seal.json'}
        if actual!=expected:raise ValueError('run artifacts changed')
        for name,digest in summary['artifact_sha256'].items():
            path=root/name
            if Path(name).is_absolute() or root not in path.resolve().parents or path.is_symlink() or sha256(path)!=digest:
                raise ValueError('run artifact digest mismatch')
        config=loads((root/'config.json').read_text(encoding='utf-8'))
        if config['batch']!=summary['batch'] or hash_object(config)!=summary['provenance']['config_sha256']:
            raise ValueError('sealed config changed')
        return summary
    except (OSError,KeyError,TypeError) as error:raise ValueError('missing or malformed run seal') from error


def run_experiment(config,output_dir,*,backend=None):
    config=validate_config(config)
    root=Path(config['raw_root']).resolve(strict=True)
    output=new_output(output_dir,(root,))
    snapshot=_raw_snapshot(root)
    if not snapshot:raise ValueError('no recognized raw CSV inputs')
    if config['evidence']['topology'] is not None:load_topology(config['evidence']['topology'])
    discovery_only=config['agents']['discovery_only']
    if not discovery_only:
        backend=backend or make_backend(config)
        if backend.model!=config['backend']['model'] or backend.metadata()['kind']!=config['backend']['kind']:
            raise ValueError('backend differs from configured experiment')
    metadata=provenance(config);started=monotonic();durations={};stages={};stage='setup'
    output.mkdir(parents=True,exist_ok=False)
    _write(output/'config.json',config)
    _write(output/'knowledge.json',knowledge_cards())
    def execute(name,operation):
        nonlocal stage
        stage=name;begin=monotonic();value=operation();durations[name]=round(monotonic()-begin,6);stages[name]=value
        return value
    try:
        profile=execute('profile',lambda:profile_dataset(root,config['batch'],**config['profile']))
        _publish_report(profile,output/'profile')
        if profile['errors']:raise ValueError('raw scan failed')
        windows=execute('features',lambda:build_features(root,config['batch'],output/'features',
            naive_timezone=config['naive_timezone'],max_rows_per_file=config['profile']['max_rows_per_file'],**config['features']))
        if snapshot!=_raw_snapshot(root) or profile['rows_scanned']!=windows['rows_scanned']:raise ValueError('raw inputs changed across scans')
        states=execute('states',lambda:discover_states(output/'features',config['batch'],output/'states',**config['states']))
        evidence=execute('evidence',lambda:build_evidence(output/'states',config['batch'],output/'evidence',**config['evidence']))
        if discovery_only:
            (output/'agents').mkdir()
            (output/'agents'/'predictions.jsonl').write_text('',encoding='utf-8')
            agents=dict(simulated=True,backend_calls=0,usage_reported_calls=0,usage_complete=True,usage={},predictions=0,
                        selection_complete=False,diagnosis_complete=False,mode='discovery_only',submission_ready=False)
            stages['agents']=agents;durations['agents']=0.0
            _write(output/'agents'/'summary.json',agents)
        else:
            args=dict(config['agents']);args.pop('discovery_only');args['budget']=Budget(**args['budget'])
            agents=execute('agents',lambda:run_diagnosis(output/'evidence'/'evidence.sqlite',config['batch'],output/'agents',backend,**args))
        stage='seal'
        if snapshot!=_raw_snapshot(root):raise ValueError('raw inputs changed during run')
        raw_fingerprint=hash_object(dict(batch=config['batch'],files=windows['files'],mode=windows['mode'],
            max_rows_per_file=windows['max_rows_per_file'],naive_timezone=config['naive_timezone']))
        usage={k:agents[k] for k in ('simulated','backend_calls','usage_reported_calls','usage_complete','usage')}
        pair_count=0
        role_path=output/'agents'/'roles.jsonl'
        if role_path.exists():
            with role_path.open(encoding='utf-8') as stream:
                for line in stream:
                    run=loads(line)
                    pair_count+=sum({'prompt_tokens','completion_tokens'}<=t['usage'].keys() for t in run['trace'])
        usage['token_pair_reported_calls']=pair_count
        partial=(not windows['complete'] or windows['mode']!='full' or not agents['selection_complete'])
        diagnosis_complete=bool(agents['diagnosis_complete'] and not partial and not discovery_only)
        summary=dict(schema_version=1,status='completed',batch=config['batch'],created_at=datetime.now(timezone.utc).isoformat(),
            output_dir=str(output),raw_root=str(root),provenance=metadata,input_fingerprint=raw_fingerprint,
            input_scope={k:windows[k] for k in ('mode','complete','max_rows_per_file','rows_scanned','naive_timezone')},
            partial=partial,diagnosis_complete=diagnosis_complete,valid_for_model_quality=not agents['simulated'] and not discovery_only,
            submission_ready=not agents['simulated'] and diagnosis_complete and not agents.get('experimental',False),
            experimental=agents.get('experimental',False),discovery_only=discovery_only,predictions=agents['predictions'],
            stages=stages,stage_elapsed_seconds=durations,elapsed_seconds=round(monotonic()-started,6),**usage)
        summary['cost']=estimate_cost(summary,config['pricing'])
        _write(output/'experiment.json',{k:v for k,v in summary.items() if k!='stages'})
        summary['artifact_sha256']={str(p.relative_to(output)):sha256(p) for p in sorted(output.rglob('*')) if p.is_file()}
        encoded=dumps(summary)+'\n'
        _write(output/'seal.json',dict(schema_version=1,summary_sha256=hashlib.sha256(encoded.encode()).hexdigest()))
        with (output/'summary.json').open('x',encoding='utf-8') as stream:stream.write(encoded)
        return summary
    except Exception as error:
        _write(output/'failure.json',dict(schema_version=1,status='failed',batch=config['batch'],stage=stage,
            error_type=type(error).__name__,completed_stages=list(stages)))
        raise ValueError('experiment failed; see sanitized failure.json') from None

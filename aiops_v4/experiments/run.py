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
        source=_external_input(b['replay_file'],'replay')
        entries=[]
        with Path(b['replay_file']).open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():entries.append(loads(line))
        if source!=_external_input(b['replay_file'],'replay'):raise ValueError('replay changed while loading')
        backend=ReplayBackend(b['model'],entries,capture_requests=False)
        backend.external_inputs=[source]
        return backend
    key=os.environ.get(b['api_key_env'])
    if not key:raise ValueError('configured API key environment is missing')
    return HTTPBackend(b['model'],b['base_url'],key,b['timeout'],b['max_response_bytes'],b['max_completion_tokens'])


def _raw_snapshot(root):
    return [dict(path=f.relative_path,source=f.source,size=f.path.stat().st_size,mtime_ns=f.path.stat().st_mtime_ns)
            for f in discover_sources(root)]


def _write(path,value):
    with path.open('x',encoding='utf-8') as stream:stream.write(dumps(value)+'\n')


def _external_input(path,kind):
    path=Path(path).resolve(strict=True);before=path.stat();digest=sha256(path);after=path.stat()
    identity=lambda stat:(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)
    if identity(before)!=identity(after):raise ValueError('external input changed while reading')
    return dict(kind=kind,path=str(path),device=after.st_dev,inode=after.st_ino,
                size=after.st_size,mtime_ns=after.st_mtime_ns,sha256=digest)


def _inventory(root):
    paths=sorted(root.rglob('*'))
    if any(p.is_symlink() for p in paths):raise ValueError('artifact symlinks forbidden')
    return {str(p.relative_to(root)):sha256(p) for p in paths if p.is_file()}


def verify_run(run_dir):
    root=Path(run_dir).resolve(strict=True)
    try:
        summary_path=root/'summary.json';seal=loads((root/'seal.json').read_text(encoding='utf-8'))
        if (root/'failure.json').exists() or sha256(summary_path)!=seal['summary_sha256']:raise ValueError('invalid run seal')
        summary=loads(summary_path.read_text(encoding='utf-8'))
        if summary['schema_version']!=1 or summary['status']!='completed':raise ValueError('run incomplete')
        if summary.get('integrity_version')!=2 or not isinstance(summary.get('external_inputs'),list):
            raise ValueError('legacy run requires a new run for verified evaluation')
        for source in summary['external_inputs']:
            if (source['kind'] not in {'topology','replay'} or not Path(source['path']).is_absolute()
                or len(source['sha256'])!=64 or type(source['device']) is not int or type(source['inode']) is not int):
                raise ValueError('invalid external input provenance')
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
    external=[]
    if config['evidence']['topology'] is not None:
        external.append(_external_input(config['evidence']['topology'],'topology'))
        load_topology(config['evidence']['topology'])
    injected=backend is not None
    discovery_only=config['agents']['discovery_only']
    if not discovery_only:
        backend=backend or make_backend(config)
        if backend.model!=config['backend']['model'] or backend.metadata()['kind']!=config['backend']['kind']:
            raise ValueError('backend differs from configured experiment')
    if not discovery_only:external.extend(getattr(backend,'external_inputs',[]))
    metadata=provenance(config)
    metadata['backend_input']=dict(injected=injected,
        replay_entries_sha256=hash_object(list(backend.entries.values())) if not discovery_only and isinstance(backend,ReplayBackend) else None)
    started=monotonic();durations={};stages={};stage='setup'
    output.mkdir(parents=True,exist_ok=False)
    _write(output/'config.json',config)
    _write(output/'knowledge.json',knowledge_cards())
    fixed=_inventory(output)
    def check_fixed():
        if _inventory(output)!=fixed:raise ValueError('published stage artifacts changed')
        if any(source!=_external_input(source['path'],source['kind']) for source in external):
            raise ValueError('external inference inputs changed')
    def capture(name):
        for relative,digest in _inventory(output/name).items():fixed[str(Path(name)/relative)]=digest
        check_fixed()
    def execute(name,operation):
        nonlocal stage
        stage=name;check_fixed();begin=monotonic();value=operation();capture(name)
        durations[name]=round(monotonic()-begin,6);stages[name]=value
        return value
    try:
        def publish_profile():
            value=profile_dataset(root,config['batch'],**config['profile'])
            _publish_report(value,output/'profile')
            return value
        profile=execute('profile',publish_profile)
        if profile['errors']:raise ValueError('raw scan failed')
        windows=execute('features',lambda:build_features(root,config['batch'],output/'features',
            naive_timezone=config['naive_timezone'],max_rows_per_file=config['profile']['max_rows_per_file'],**config['features']))
        file_keys=('path','source','columns','rows_scanned','complete','parsed_records_sha256')
        scanned_files=lambda files:sorted(({k:row[k] for k in file_keys} for row in files),key=lambda row:row['path'])
        if (snapshot!=_raw_snapshot(root) or profile['rows_scanned']!=windows['rows_scanned']
            or scanned_files(profile['files'])!=scanned_files(windows['files'])):
            raise ValueError('raw inputs changed across scans')
        states=execute('states',lambda:discover_states(output/'features',config['batch'],output/'states',**config['states']))
        evidence=execute('evidence',lambda:build_evidence(output/'states',config['batch'],output/'evidence',**config['evidence']))
        if discovery_only:
            stage='agents';check_fixed()
            (output/'agents').mkdir()
            (output/'agents'/'predictions.jsonl').write_text('',encoding='utf-8')
            agents=dict(simulated=True,backend_calls=0,usage_reported_calls=0,usage_complete=True,usage={},predictions=0,
                        selection_complete=False,diagnosis_complete=False,mode='discovery_only',submission_ready=False)
            stages['agents']=agents;durations['agents']=0.0
            _write(output/'agents'/'summary.json',agents)
            capture('agents')
        else:
            args=dict(config['agents']);args.pop('discovery_only');args['budget']=Budget(**args['budget'])
            agents=execute('agents',lambda:run_diagnosis(output/'evidence'/'evidence.sqlite',config['batch'],output/'agents',backend,**args))
        stage='seal';check_fixed()
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
        summary=dict(schema_version=1,integrity_version=2,external_inputs=external,status='completed',batch=config['batch'],created_at=datetime.now(timezone.utc).isoformat(),
            output_dir=str(output),raw_root=str(root),provenance=metadata,input_fingerprint=raw_fingerprint,
            input_scope={k:windows[k] for k in ('mode','complete','max_rows_per_file','rows_scanned','naive_timezone')},
            partial=partial,diagnosis_complete=diagnosis_complete,valid_for_model_quality=not agents['simulated'] and not discovery_only,
            submission_ready=not agents['simulated'] and diagnosis_complete and not agents.get('experimental',False),
            experimental=agents.get('experimental',False),discovery_only=discovery_only,predictions=agents['predictions'],
            stages=stages,stage_elapsed_seconds=durations,elapsed_seconds=round(monotonic()-started,6),**usage)
        summary['cost']=estimate_cost(summary,config['pricing'])
        _write(output/'experiment.json',{k:v for k,v in summary.items() if k!='stages'})
        fixed['experiment.json']=sha256(output/'experiment.json');check_fixed()
        summary['artifact_sha256']=dict(fixed)
        encoded=dumps(summary)+'\n'
        _write(output/'seal.json',dict(schema_version=1,summary_sha256=hashlib.sha256(encoded.encode()).hexdigest()))
        with (output/'summary.json').open('x',encoding='utf-8') as stream:stream.write(encoded)
        return summary
    except (Exception,KeyboardInterrupt) as error:
        interrupted=isinstance(error,KeyboardInterrupt)
        _write(output/'failure.json',dict(schema_version=1,status='interrupted' if interrupted else 'failed',
            batch=config['batch'],stage=stage,error_type=type(error).__name__,completed_stages=list(stages)))
        if interrupted:raise
        raise ValueError('experiment failed; see sanitized failure.json') from None

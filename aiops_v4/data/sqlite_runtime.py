"""Audited performance-only adapters for explicitly selected canonical stores.

This helper deliberately leaves the producer files, signatures, schemas,
transactions, SQL statements, ordering, codecs and resource guards unchanged.
No sqlite3.connect global replacement is used. Only named application hooks are
adapted, and only the exact allowed paths below may acquire tuned cache policy.
"""
from pathlib import Path
import functools
import hashlib
import json
import multiprocessing
import os
import re
import time

_ACTIVE=None


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_profile(value):
    fields={'schema_version','allowed_database_roots','main_writer_cache_bytes',
        'wal_autocheckpoint_pages','reader_cache_bytes','evidence_cache_bytes',
        'memory_budget_bytes','workers','audit_dir','source_manifest_path','source_manifest_sha256',
        'runtime_source_path','runtime_source_sha256'}
    if not isinstance(value,dict) or set(value)!=fields or value['schema_version']!=1:
        raise ValueError('unsupported explicit SQLite runtime profile')
    for name in ('main_writer_cache_bytes','wal_autocheckpoint_pages','reader_cache_bytes',
                 'evidence_cache_bytes','memory_budget_bytes','workers'):
        if type(value[name]) is not int or value[name]<=0:raise ValueError('invalid positive profile integer: '+name)
    budget=value['memory_budget_bytes'];workers=value['workers']
    if workers>8 or budget<4*1024**3:raise ValueError('unsupported memory/worker budget')
    if value['main_writer_cache_bytes']>budget//4 or value['main_writer_cache_bytes']<1024:
        raise ValueError('main cache exceeds one quarter of run memory budget')
    if value['reader_cache_bytes']>min(64*1024**2,budget//(workers*32)):
        raise ValueError('reader cache does not fit worker memory cap')
    if value['evidence_cache_bytes']>min(256*1024**2,budget//128):
        raise ValueError('evidence cache exceeds its independent 2 GiB guard reserve')
    if not 1000<=value['wal_autocheckpoint_pages']<=262144:raise ValueError('unbounded WAL checkpoint threshold')
    roots=value['allowed_database_roots']
    if not isinstance(roots,list) or not roots or any(not isinstance(r,str) or not Path(r).is_absolute() for r in roots):
        raise ValueError('explicit absolute database roots required')
    if len(set(roots))!=len(roots):raise ValueError('duplicate allowed roots')
    for key in ('audit_dir','source_manifest_path','runtime_source_path'):
        if not isinstance(value[key],str) or not Path(value[key]).is_absolute():raise ValueError('absolute audit/manifest paths required')
    if Path(value['runtime_source_path']).resolve()!=Path(__file__).resolve() or digest(__file__)!=value['runtime_source_sha256']:
        raise ValueError('runtime source hash/path differs from explicit profile')
    manifest=Path(value['source_manifest_path'])
    if digest(manifest)!=value['source_manifest_sha256']:raise ValueError('frozen source manifest hash changed')
    repo=Path.cwd().resolve()
    source=json.loads(manifest.read_text())
    if not isinstance(source,dict) or len(source)<1:raise ValueError('invalid frozen source manifest')
    for relative,expected in source.items():
        path=(repo/relative).resolve()
        if repo not in path.parents or digest(path)!=expected:raise ValueError('frozen production source changed: '+relative)
    return dict(value)


def main_database(db):
    row=next((row for row in db.execute('PRAGMA database_list') if row[1]=='main'),None)
    return Path(row[2]).resolve() if row is not None and row[2] else None


def owned_role(db,profile):
    path=main_database(db)
    if path is None:return None
    for root in profile['allowed_database_roots']:
        try:relative=path.relative_to(Path(root).resolve())
        except ValueError:continue
        if len(relative.parts)!=2:continue
        layer,file=relative.parts
        match=re.fullmatch(r'(features|states|evidence)(?:_[0-9a-f]{16})?',layer)
        if not match:continue
        kind=match.group(1)
        if kind in ('features','states') and file=='.'+kind+'.sqlite':return 'formal_writer'
        if kind=='evidence' and file=='.evidence.sqlite':return 'evidence_writer'
        if kind in ('features','states') and file==kind+'.sqlite':return 'sealed_reader'
    return None


def audit(profile,db,role):
    row=dict(time=time.time(),pid=os.getpid(),process=multiprocessing.current_process().name,
        role=role,database=str(main_database(db)),cache_size=db.execute('PRAGMA cache_size').fetchone()[0],
        synchronous=db.execute('PRAGMA synchronous').fetchone()[0],
        journal_mode=db.execute('PRAGMA journal_mode').fetchone()[0],
        wal_autocheckpoint=db.execute('PRAGMA wal_autocheckpoint').fetchone()[0],
        profile_sha256=_ACTIVE['profile_sha256'],runtime_sha256=digest(__file__))
    directory=Path(profile['audit_dir']);directory.mkdir(parents=True,exist_ok=True)
    with (directory/('connections_'+str(os.getpid())+'.jsonl')).open('a') as stream:
        stream.write(json.dumps(row,sort_keys=True)+'\n')


def install(profile_path):
    global _ACTIVE
    profile_path=Path(profile_path).resolve(strict=True);hashed=digest(profile_path)
    if _ACTIVE is not None:
        if _ACTIVE['profile_sha256']!=hashed:raise ValueError('runtime profile cannot change within a running process')
        return _ACTIVE['profile']
    profile=validate_profile(json.loads(profile_path.read_text()))
    import aiops_v4.features.store as fs
    import aiops_v4.features.build as fb
    import aiops_v4.states.store as ss
    import aiops_v4.states.build as sb
    import aiops_v4.evidence.index as ei
    import aiops_v4.evidence.pipeline as ep
    import aiops_v4.evidence.linked as el
    original_write=fs.configure_write_connection
    original_build=fs.configure_build_connection
    original_feature_init=fs.FeatureStore.__init__
    original_state_init=ss.StateStore.__init__
    original_create_index=ei.create_index
    original_attach=el.attach_linked
    _ACTIVE=dict(profile=profile,profile_sha256=hashed,profile_path=str(profile_path))

    def main_writer(db):
        return multiprocessing.current_process().name=='MainProcess' and owned_role(db,profile)=='formal_writer'

    @functools.wraps(original_write)
    def configure_write(db,*,memory_bytes=None,workers=1,cache_bytes=None):
        tuned=main_writer(db)
        if tuned:cache_bytes=profile['main_writer_cache_bytes']
        result=original_write(db,memory_bytes=memory_bytes,workers=workers,cache_bytes=cache_bytes)
        if tuned:audit(profile,db,'formal_writer_cache')
        return result

    @functools.wraps(original_build)
    def configure_build(db,*,memory_bytes=None,workers=1,cache_bytes=None):
        tuned=main_writer(db)
        if tuned:cache_bytes=profile['main_writer_cache_bytes']
        result=original_build(db,memory_bytes=memory_bytes,workers=workers,cache_bytes=cache_bytes)
        if tuned:
            db.execute('PRAGMA wal_autocheckpoint='+str(profile['wal_autocheckpoint_pages']))
            audit(profile,db,'formal_writer_wal')
        return result

    def tune_reader(db):
        if owned_role(db,profile)=='sealed_reader' and db.execute('PRAGMA query_only').fetchone()[0]==1:
            db.execute('PRAGMA cache_size=-'+str(profile['reader_cache_bytes']//1024))
            audit(profile,db,'sealed_reader')

    @functools.wraps(original_feature_init)
    def feature_init(self,*args,**kwargs):
        original_feature_init(self,*args,**kwargs)
        try:tune_reader(self.db)
        except BaseException:self.db.close();raise

    @functools.wraps(original_state_init)
    def state_init(self,*args,**kwargs):
        original_state_init(self,*args,**kwargs)
        try:tune_reader(self.db)
        except BaseException:self.close();raise

    def tune_evidence(db):
        if multiprocessing.current_process().name=='MainProcess' and owned_role(db,profile)=='evidence_writer':
            # Linked evidence owns a transaction before attach_linked. Changing
            # journal/sync inside it is unsafe; preserve DELETE and assert FULL.
            if db.in_transaction:
                if db.execute('PRAGMA synchronous').fetchone()[0]!=2:
                    raise ValueError('evidence transaction is not FULL durable')
            else:db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA cache_size=-'+str(profile['evidence_cache_bytes']//1024))
            audit(profile,db,'evidence_writer_cache')

    @functools.wraps(original_create_index)
    def create_index(*args,**kwargs):
        db=original_create_index(*args,**kwargs)
        try:tune_evidence(db)
        except BaseException:db.close();raise
        return db

    @functools.wraps(original_attach)
    def attach_linked(db,*args,**kwargs):
        tune_evidence(db)
        return original_attach(db,*args,**kwargs)

    fs.configure_write_connection=configure_write
    fs.configure_build_connection=configure_build
    fb.configure_build_connection=configure_build
    sb.configure_build_connection=configure_build
    fs.FeatureStore.__init__=feature_init
    ss.StateStore.__init__=state_init
    ei.create_index=create_index
    ep.create_index=create_index
    el.attach_linked=attach_linked
    directory=Path(profile['audit_dir']);directory.mkdir(parents=True,exist_ok=True)
    record=dict(pid=os.getpid(),process=multiprocessing.current_process().name,time=time.time(),
        profile_path=str(profile_path),profile_sha256=hashed,runtime_sha256=digest(__file__),
        source_manifest_sha256=profile['source_manifest_sha256'],patched_bindings=[
        'features.store.configure_write_connection','features.store.configure_build_connection',
        'features.build.configure_build_connection','states.build.configure_build_connection',
        'FeatureStore.__init__','StateStore.__init__','evidence.index.create_index',
        'evidence.pipeline.create_index','evidence.linked.attach_linked'])
    (directory/('activation_'+str(os.getpid())+'.json')).write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    return profile


def install_from_environment():
    path=os.environ.get('AIOPS_SQLITE_TUNING_PROFILE')
    if not path:return None
    expected=os.environ.get('AIOPS_SQLITE_TUNING_PROFILE_SHA256')
    if not expected or digest(path)!=expected:raise ValueError('inherited runtime profile hash changed')
    return install(path)

"""Atomic layer seals and producer-specific invalidation, independent of LLM settings."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

DEPENDENCIES = {
 'features':('data/position.py','data/reader.py','data/discovery.py','data/contracts.py','data/essential.py','features/build.py','features/spool.py','features/spool_codec.py','features/source_stream.py','features/store.py','features/write_codec.py','features/aggregate.py','features/netflow.py','features/flow_codec.py','features/block_codec.py','features/ingest_batch.py','features/series.py','features/time.py','features/identity.py','features/semantics.py','data/statistics.py','storage.py','data/resources.py','data/workers.py','data/lock.py','data/cache.py'),
 'states':('states/build.py','states/vector_batch.py','states/blocks.py','states/store.py','states/codec.py','states/blocks.py','states/matrix.py','states/reference.py','states/cluster.py','states/signals.py','states/events.py','states/selection.py','states/pipeline.py','features/store.py','features/write_codec.py','features/block_codec.py','storage.py','data/resources.py','data/workers.py','data/lock.py','data/cache.py'),
 'evidence':('evidence/linked.py','evidence/pipeline.py','evidence/association.py','evidence/packet.py','evidence/raw.py','evidence/index.py','states/store.py','states/codec.py','states/blocks.py','states/matrix.py','features/store.py','features/write_codec.py','data/position.py','storage.py','data/resources.py','data/workers.py','data/lock.py','data/cache.py')}
_VERIFIED={}


def sha256(path,end=None):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        remaining=end
        while remaining is None or remaining>0:
            chunk=stream.read(1024*1024 if remaining is None else min(1024*1024,remaining))
            if not chunk:break
            h.update(chunk)
            if remaining is not None:remaining-=len(chunk)
        if remaining:raise ValueError('raw input shorter than scanned range')
    return h.hexdigest()


def object_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()


def producer_hash(layer):
    root=Path(__file__).resolve().parents[1]
    files={n:sha256(root/n) for n in DEPENDENCIES[layer] if (root/n).is_file()}
    # Identity and rule interpretation also depend on public configuration.
    common=root.parent/'aiops_challenge_2026'
    for p in sorted(common.rglob('*.json')):files[str(p.relative_to(root.parent))]=sha256(p)
    for p in [*sorted((common/'config').rglob('*.py')),root.parent/'aiops_common/data/observations.py']:
        files[str(p.relative_to(root.parent))]=sha256(p)
    return object_hash(files)


def layer_signature(layer,config,upstream):
    selected={'batch':config['batch'],layer:config.get(layer,{})}
    if layer=='features':
        selected.update(raw_root=config.get('raw_root'),naive_timezone=config['naive_timezone'],max_rows_per_file=config.get('profile',{}).get('max_rows_per_file'))
    return dict(format=layer+'_v1',producer_sha256=producer_hash(layer),config_sha256=object_hash(selected),upstream=upstream)


def atomic_json(path,value):
    path=Path(path)
    fd,temp=tempfile.mkstemp(prefix='.publish-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(value,out,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'));out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(temp,path)
    finally:
        if Path(temp).exists():Path(temp).unlink()


def dependency_path(directory,relative):
    directory=Path(directory).resolve();root=directory.parent
    if not isinstance(relative,str) or not relative or Path(relative).is_absolute():raise ValueError('unsafe dependency path')
    candidate=(directory/relative).resolve()
    if candidate==root or root not in candidate.parents or (directory/relative).is_symlink():raise ValueError('dependency outside sealed dataset root')
    if not candidate.is_file():raise ValueError('missing sealed dependency')
    return candidate


def seal_layer(directory,summary,*,signature,dependencies=None):
    directory=Path(directory)
    if (directory/'seal.json').exists() or (directory/'summary.json').exists():raise FileExistsError('layer already published')
    files={p.name:sha256(p) for p in sorted(directory.iterdir()) if p.is_file() and not p.name.startswith('.')}
    if any(p.is_symlink() for p in directory.iterdir()):raise ValueError('layer symlink forbidden')
    summary=dict(summary,layer_signature=signature,dependencies=dependencies or {},artifact_sha256=files)
    atomic_json(directory/'summary.json',summary)
    atomic_json(directory/'seal.json',dict(format='canonical_layer_seal_v1',summary_sha256=sha256(directory/'summary.json')))
    return summary


def checked_digest(path,digest):
    p=Path(path);before=p.stat()
    key=(str(p.resolve()),before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
    if _VERIFIED.get(key)==digest:return
    after=p.stat()
    if sha256(p)!=digest:raise ValueError('layer artifact/upstream digest mismatch')
    after=p.stat()
    if (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=key[1:]:raise ValueError('layer changed during digest verification')
    if len(_VERIFIED)>256:_VERIFIED.clear()
    _VERIFIED[key]=digest


def _verify_contents(directory,summary,verify_raw):
    expected=set(summary['artifact_sha256'])|{'summary.json'}
    actual={p.name for p in directory.iterdir() if p.is_file() and not p.name.startswith('.')} - {'seal.json'}
    if expected!=actual or any(p.is_symlink() for p in directory.iterdir()):raise ValueError('layer inventory changed')
    for name,digest in summary['artifact_sha256'].items():
        if Path(name).name!=name:raise ValueError('unsafe artifact path')
        checked_digest(directory/name,digest)
    for relative,digest in summary.get('dependencies',{}).items():checked_digest(dependency_path(directory,relative),digest)
    if verify_raw:
        from .position import file_identity
        root=Path(summary['input_root']).resolve(strict=True)
        for entry in summary['files']:
            p=dependency_raw(root,entry['path'])
            if file_identity(p)!=entry['identity'] or sha256(p,entry['byte_range_end'])!=entry['raw_bytes_sha256']:raise ValueError('raw input changed')


def recover_layer(directory,signature,*,verify_raw=False):
    """Complete a prepared publication only after validating its frozen bytes."""
    directory=Path(directory)
    marker=json.loads((directory/'.build.json').read_text())
    summary=json.loads((directory/'summary.json').read_text())
    if marker['signature']!=signature or summary['layer_signature']!=signature:raise ValueError('prepared producer/config/input changed')
    _verify_contents(directory,summary,verify_raw)
    atomic_json(directory/'seal.json',dict(format='canonical_layer_seal_v1',summary_sha256=sha256(directory/'summary.json')))
    return verify_layer(directory,verify_raw=verify_raw)


def verify_layer(directory,*,verify_raw=False):
    directory=Path(directory).resolve(strict=True)
    if directory.is_symlink():raise ValueError('unsafe layer directory')
    summary=json.loads((directory/'summary.json').read_text());seal=json.loads((directory/'seal.json').read_text())
    if seal.get('format')!='canonical_layer_seal_v1' or sha256(directory/'summary.json')!=seal['summary_sha256']:raise ValueError('layer summary digest mismatch')
    _verify_contents(directory,summary,verify_raw)
    return summary


def dependency_raw(root,relative):
    p=Path(relative)
    if p.is_absolute() or '..' in p.parts:raise ValueError('unsafe raw path')
    target=Path(root)/p
    if target.is_symlink() or Path(root).resolve() not in target.resolve(strict=True).parents:raise ValueError('unsafe raw path')
    return target

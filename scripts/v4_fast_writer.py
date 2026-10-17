"""Explicit fast-disk staging for canonical writers, with durable publication.

The source package and feature producer signature are unchanged. Only owned,
unsealed feature/state database paths can be staged. Raw input and parser
spools stay in the dataset. FULL durability is retained on the fast disk.
"""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
_ACTIVE = None


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.transition-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _allocated(root):
    total = 0
    if root.exists():
        for path in root.rglob('*'):
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode):
                    total += info.st_blocks * 512
            except FileNotFoundError:
                pass
    return total


def _digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(4 * 1024**2), b''):
            value.update(data)
    return value.hexdigest()


def _deleted_bytes(root):
    if not sys.platform.startswith('linux'):
        return 0
    parent = multiprocessing.parent_process()
    pending = [parent.pid if parent else os.getpid()]
    seen, files = set(), {}
    prefix = str(root) + '/'
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        proc = Path('/proc') / str(pid)
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            pending.extend(map(int, (proc / 'task' / str(pid) / 'children').read_text().split()))
            for fd in (proc / 'fd').iterdir():
                try:
                    target = os.readlink(fd)
                    if target.startswith(prefix) and target.endswith(' (deleted)'):
                        info = fd.stat()
                        if stat.S_ISREG(info.st_mode):
                            files[info.st_dev, info.st_ino] = info.st_blocks * 512
                except OSError:
                    pass
        except (OSError, ValueError):
            pass
    return sum(files.values())


class FastWriter:
    def __init__(self, data_root, fast_root, *, min_free_bytes=12 * 1024**3,
                 max_fast_bytes=40 * 1024**3, defer_state_digest_index=False):
        self.data_root = Path(data_root).absolute()
        self.fast_root = Path(fast_root).absolute()
        for root in (self.data_root, self.fast_root):
            if root.is_symlink() or root.resolve() != root:
                raise ValueError('fast writer roots must be canonical ordinary directories')
        if (self.data_root == self.fast_root or self.data_root in self.fast_root.parents
                or self.fast_root in self.data_root.parents):
            raise ValueError('fast and persistent roots must be separate')
        if type(min_free_bytes) is not int or min_free_bytes < 0:
            raise ValueError('invalid fast reserve')
        if type(max_fast_bytes) is not int or max_fast_bytes < 1:
            raise ValueError('invalid fast workspace budget')
        if type(defer_state_digest_index) is not bool:
            raise ValueError('invalid state index policy')
        self.min_free_bytes = min_free_bytes
        self.max_fast_bytes = max_fast_bytes
        self.defer_state_digest_index = defer_state_digest_index
        self.resource_policy = None
        self.fast_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.fast_root.stat().st_uid != os.getuid():
            raise ValueError('fast workspace must be owned')

    def owns(self, path):
        path = Path(path).absolute()
        if path.parent.parent != self.data_root:
            return False
        match = re.fullmatch(r'(features|states)(?:_[0-9a-f]{16})?', path.parent.name)
        return bool(match and path.name == '.' + match.group(1) + '.sqlite')

    def _producer(self):
        try:
            owner = json.loads((self.data_root / '.producer.lock').read_text())
        except (OSError, ValueError):
            raise RuntimeError('owned producer lock is required before staging') from None
        if owner.get('pid') != os.getpid() or owner.get('uid') != os.getuid():
            raise RuntimeError('another producer owns the dataset')

    def check(self, extra_bytes=0):
        used = _allocated(self.fast_root) + _deleted_bytes(self.fast_root)
        if used + extra_bytes > self.max_fast_bytes:
            raise RuntimeError('fast workspace ceiling exhausted; checkpoint retained')
        if shutil.disk_usage(self.fast_root).free < self.min_free_bytes + extra_bytes:
            raise RuntimeError('fast disk reserve exhausted; checkpoint retained')
        return used

    def _copy(self, source, destination, *, on_fast):
        before = source.stat()
        for leftover in destination.parent.glob('.copy-*'):
            info = leftover.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                leftover.unlink()
        if on_fast:
            self.check(before.st_size)
        else:
            self._publication_room(destination, before.st_size)
        fd, name = tempfile.mkstemp(prefix='.copy-', dir=destination.parent)
        digest = hashlib.sha256()
        started = time.monotonic()
        try:
            with source.open('rb') as inp, os.fdopen(fd, 'wb') as out:
                checked = 0
                while True:
                    data = inp.read(4 * 1024**2)
                    if not data:
                        break
                    digest.update(data)
                    out.write(data)
                    if out.tell() - checked >= 64 * 1024**2:
                        checked = out.tell()
                        if on_fast:
                            self.check()
                        else:
                            self._publication_room(destination, max(0, before.st_size - checked))
                out.flush()
                os.fsync(out.fileno())
            after = source.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise RuntimeError('source changed during fast writer copy')
            os.replace(name, destination)
            _sync_directory(destination.parent)
            return dict(bytes=before.st_size, sha256=digest.hexdigest(),
                        seconds=time.monotonic() - started)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _publication_room(self, destination, remaining):
        reserve = self.min_free_bytes
        if self.resource_policy is not None:
            reserve = max(reserve, self.resource_policy.min_free_bytes)
            used = (_allocated(self.data_root) + _deleted_bytes(self.data_root)
                    + _allocated(self.fast_root) + _deleted_bytes(self.fast_root))
            if used + remaining > self.resource_policy.max_workspace_bytes:
                raise RuntimeError('combined workspace ceiling exhausted; fast checkpoint retained')
        if shutil.disk_usage(destination.parent).free < reserve + remaining:
            raise RuntimeError('persistent disk reserve exhausted; fast checkpoint retained')

    def _paths(self, logical):
        if not self.owns(logical):
            raise ValueError('path is not an owned unsealed writer')
        if logical.parent.is_symlink():
            raise ValueError('layer symlink is forbidden')
        layer = self.fast_root / logical.parent.name
        if layer.is_symlink():
            raise ValueError('fast layer symlink is forbidden')
        layer.mkdir(exist_ok=True, mode=0o700)
        return layer / logical.name, layer / 'transition.json'

    def _link(self, logical, fast):
        if logical.is_symlink():
            if logical.resolve() != fast:
                raise ValueError('unregistered writer symlink')
            return
        if logical.exists():
            raise RuntimeError('cannot replace an existing writer during recovery')
        name = logical.with_name(logical.name + '.fast-link')
        if name.exists() or name.is_symlink():
            if not name.is_symlink() or name.resolve() != fast:
                raise RuntimeError('unresolved fast writer link transition')
        else:
            name.symlink_to(fast)
        os.replace(name, logical)
        _sync_directory(logical.parent)

    def stage(self, path):
        self._producer()
        logical = Path(path).absolute()
        fast, ledger = self._paths(logical)
        if logical.is_symlink():
            if not ledger.exists() or logical.resolve() != fast or not fast.is_file():
                raise ValueError('unregistered writer symlink')
            return fast
        if ledger.exists():
            saved = json.loads(ledger.read_text())
            if saved.get('logical') != str(logical) or saved.get('fast') != str(fast):
                raise ValueError('fast writer workspace belongs to another dataset')
            if saved['status'] == 'published':
                return logical
            if fast.exists():
                self.recover()
                return fast
        backup = logical.parent / '.before_fast_writer'
        if backup.exists():
            raise RuntimeError('unresolved original checkpoint backup')
        copied = {}
        for leftover in fast.parent.glob('.copy-*'):
            info = leftover.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                leftover.unlink()
        if logical.exists():
            if not logical.is_file() or logical.stat().st_uid != os.getuid():
                raise ValueError('writer must be an owned regular file')
            for suffix in ('', '-wal'):
                source = Path(str(logical) + suffix)
                destination = Path(str(fast) + suffix)
                if source.exists():
                    if destination.exists() and not destination.is_symlink() and _digest(destination) == _digest(source):
                        copied[suffix] = dict(bytes=source.stat().st_size, sha256=_digest(destination), reused_orphan=True)
                    else:
                        if destination.exists() and not destination.is_symlink():
                            destination.unlink()
                        copied[suffix] = self._copy(source, destination, on_fast=True)
                elif destination.exists() and not destination.is_symlink():
                    destination.unlink()
        else:
            fast.touch(mode=0o600)
        record = dict(format='fast_writer_transition_v1', logical=str(logical), fast=str(fast),
                      status='cloned', original_copy=copied, pid=os.getpid(), time=time.time())
        _atomic_json(ledger, record)
        backup.mkdir(mode=0o700)
        for suffix in ('', '-wal', '-shm'):
            source = Path(str(logical) + suffix)
            if source.exists():
                source.rename(backup / source.name)
        _sync_directory(backup)
        _sync_directory(logical.parent)
        self._link(logical, fast)
        record['status'] = 'staged'
        _atomic_json(ledger, record)
        return fast

    def recover(self):
        self._producer()
        # A killed copy may predate its ledger. These are private clone files;
        # reclaim them before the canonical pipeline checks workspace budgets.
        for layer in self.fast_root.iterdir():
            if layer.is_dir() and not layer.is_symlink() and re.fullmatch(r'(features|states)(?:_[0-9a-f]{16})?', layer.name):
                for leftover in layer.glob('.copy-*'):
                    info = leftover.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                        leftover.unlink()
        for ledger in self.fast_root.glob('*/transition.json'):
            saved = json.loads(ledger.read_text())
            logical = Path(saved['logical'])
            fast, expected_ledger = self._paths(logical)
            if ledger != expected_ledger or saved['fast'] != str(fast):
                raise ValueError('invalid fast writer transition location')
            for directory in (logical.parent, fast.parent):
                for leftover in directory.glob('.copy-*'):
                    info = leftover.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                        leftover.unlink()
            if saved['status'] == 'published' and fast.exists():
                persistent = logical if logical.exists() else logical.with_name(logical.name.lstrip('.'))
                expected = saved['published_copy']['sha256']
                if persistent.is_symlink() or not persistent.is_file() or _digest(persistent) != expected or _digest(fast) != expected:
                    raise RuntimeError('cannot verify published writer before cleanup')
                fast.unlink()
                _sync_directory(fast.parent)
                continue
            if saved['status'] == 'publishing' and logical.is_file() and not logical.is_symlink():
                if not fast.exists() or _digest(logical) != _digest(fast):
                    raise RuntimeError('published copy differs; fast checkpoint retained')
                saved['published_copy'] = dict(bytes=fast.stat().st_size, sha256=_digest(fast),
                                              verified_recovery=True)
                saved['status'] = 'published'
                _atomic_json(ledger, saved)

                fast.unlink()
                _sync_directory(fast.parent)
                continue
            if saved['status'] in ('cloned', 'staged', 'publishing'):
                if not fast.is_file():
                    raise RuntimeError('staged writer is missing; original checkpoint retained')
                if saved['status'] == 'cloned':
                    backup = logical.parent / '.before_fast_writer'
                    backup.mkdir(exist_ok=True, mode=0o700)
                    for suffix in ('', '-wal', '-shm'):
                        source = Path(str(logical) + suffix)
                        if suffix == '' and source.is_symlink() and source.resolve() == fast:
                            continue
                        if source.exists():
                            expected = saved['original_copy'].get(suffix)
                            if suffix != '-shm' and (expected is None or _digest(source) != expected['sha256']):
                                raise RuntimeError('original checkpoint changed during interrupted transition')
                            destination = backup / source.name
                            if destination.exists():
                                raise RuntimeError('checkpoint backup already exists')
                            source.rename(destination)
                    _sync_directory(backup)
                self._link(logical, fast)
                saved['status'] = 'staged'
                _atomic_json(ledger, saved)

    def monitored_path(self, path):
        path = Path(path).absolute()
        if not path.is_symlink():
            return path
        if not self.owns(path):
            raise ValueError('unregistered monitored symlink')
        fast = self.fast_root / path.parent.name / path.name
        ledger = fast.parent / 'transition.json'
        value = json.loads(ledger.read_text())
        if (value.get('logical') != str(path) or value.get('fast') != str(fast)
                or path.resolve() != fast or fast.is_symlink()
                or value.get('status') not in ('cloned', 'staged', 'publishing')):
            raise ValueError('unregistered monitored symlink')
        return fast

    def publish(self, db, finalize):
        actual = Path(next(row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main')).resolve()
        if actual.parent.parent != self.fast_root:
            return finalize(db)
        ledger = actual.parent / 'transition.json'
        saved = json.loads(ledger.read_text())
        logical = Path(saved['logical'])
        fast, expected_ledger = self._paths(logical)
        if actual != fast or ledger != expected_ledger or logical.resolve() != fast:
            raise ValueError('publication does not match registered writer')
        if shutil.disk_usage(logical.parent).free < fast.stat().st_size + self.min_free_bytes:
            raise RuntimeError('persistent disk reserve exhausted; fast checkpoint retained')
        finalize(db)
        db.close()
        for leftover in logical.parent.glob('.copy-*'):
            info = leftover.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                leftover.unlink()
        self._publication_room(logical, fast.stat().st_size)
        saved['status'] = 'publishing'
        _atomic_json(ledger, saved)
        saved['published_copy'] = self._copy(fast, logical, on_fast=False)
        saved['status'] = 'published'
        saved['published_at'] = time.time()
        _atomic_json(ledger, saved)
        fast.unlink()
        _sync_directory(fast.parent)

    def install(self):
        """Adapt application bindings; never replace sqlite3.connect globally."""
        from aiops_v4.features import store as fs, build as fb
        from aiops_v4.states import build as sb
        from aiops_v4.data import pipeline, resources, cache
        original_finalize = fs.finalize_build_connection
        original_check = resources.ResourcePolicy.check
        original_configure = resources.ResourcePolicy.configure_sqlite
        original_pipeline = pipeline._build_data_layers
        original_hash = cache.producer_hash
        writer = self

        class StateConnection(sqlite3.Connection):
            def executescript(self, sql, /):
                if writer.defer_state_digest_index:
                    sql = sql.replace('CREATE TABLE IF NOT EXISTS deltas(id BLOB UNIQUE NOT NULL,',
                                      'CREATE TABLE IF NOT EXISTS deltas(id BLOB NOT NULL,')
                return super().executescript(sql)

        class SQLiteBindings:
            def __getattr__(self, name):
                return getattr(sqlite3, name)

            def connect(self, database, *args, **kwargs):
                text = str(database)
                if (not text.startswith('file:') and text != ':memory:'
                        and multiprocessing.current_process().name == 'MainProcess'
                        and writer.owns(text)):
                    writer.stage(text)
                    if Path(text).name == '.states.sqlite' and writer.defer_state_digest_index:
                        if 'factory' in kwargs:
                            raise ValueError('custom state connection factory is unsupported')
                        kwargs['factory'] = StateConnection
                return sqlite3.connect(database, *args, **kwargs)

        def finalize(db):
            actual = Path(next(row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main')).resolve()
            if (writer.defer_state_digest_index and actual.parent.parent == writer.fast_root
                    and actual.name == '.states.sqlite'):
                sql = 'CREATE UNIQUE INDEX IF NOT EXISTS delta_digest ON deltas(id)'
                if writer.resource_policy is not None:
                    with writer.resource_policy.guard_sql(db):
                        db.execute(sql)
                else:
                    writer.check()
                    db.execute(sql)
                db.commit()
            return writer.publish(db, original_finalize)

        def check(policy):
            used = original_check(policy)
            if policy.root.resolve() == writer.data_root:
                used += writer.check()
                if used > policy.max_workspace_bytes:
                    raise resources.WorkspaceBudgetError('combined workspace ceiling exhausted; checkpoint retained')
                policy.peak_bytes = max(policy.peak_bytes, used)
                if multiprocessing.current_process().name == 'MainProcess':
                    writer.resource_policy = policy
            return used

        def configure(policy):
            original_configure(policy)
            if policy.root.resolve() == writer.data_root:
                temp = writer.fast_root / '.sqlite-temp'
                temp.mkdir(exist_ok=True)
                with sqlite3.connect(':memory:') as db:
                    db.execute("PRAGMA temp_store_directory='" + str(temp).replace("'", "''") + "'")
                    got = db.execute('PRAGMA temp_store_directory').fetchone()
                    if got is None or Path(got[0]) != temp:
                        raise RuntimeError('cannot confine SQLite sorts to fast workspace')

        def build(config, directory, **kwargs):
            if Path(directory).resolve() == writer.data_root:
                writer.recover()
            return original_pipeline(config, directory, **kwargs)

        implementation_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        def producer_hash(layer):
            base = original_hash(layer)
            if writer.defer_state_digest_index and layer in ('states', 'evidence'):
                return cache.object_hash(dict(base=base, fast_state_index_v1=implementation_hash))
            return base

        bindings = SQLiteBindings()
        for module in (fs, fb, sb):
            module.sqlite3 = bindings
            module.finalize_build_connection = finalize
        resources.ResourcePolicy.check = check
        resources.ResourcePolicy.configure_sqlite = configure
        pipeline._build_data_layers = build
        cache.producer_hash = producer_hash


def install_from_environment():
    global _ACTIVE
    path = os.environ.get('AIOPS_FAST_WRITER_PROFILE')
    if not path:
        raise ValueError('explicit fast writer profile is required')
    payload = Path(path).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != os.environ.get('AIOPS_FAST_WRITER_PROFILE_SHA256'):
        raise ValueError('fast writer profile changed')
    if _ACTIVE is not None:
        return _ACTIVE
    value = json.loads(payload)
    fields = {'schema_version', 'data_root', 'fast_root', 'min_free_bytes',
              'max_fast_bytes', 'defer_state_digest_index', 'implementation_sha256'}
    if set(value) != fields or value['schema_version'] != 1:
        raise ValueError('unsupported fast writer profile')
    if value['implementation_sha256'] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError('fast writer implementation changed')
    writer = FastWriter(value['data_root'], value['fast_root'],
                        min_free_bytes=value['min_free_bytes'], max_fast_bytes=value['max_fast_bytes'],
                        defer_state_digest_index=value['defer_state_digest_index'])
    writer.install()
    _ACTIVE = writer
    return writer


def main():
    import argparse
    import runpy
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, help='existing explicit SQLite cache profile')
    parser.add_argument('--fast-profile', type=Path)
    parser.add_argument('-m', dest='module', required=True)
    args, remaining = parser.parse_known_args()
    if args.fast_profile is not None:
        path = str(args.fast_profile.resolve(strict=True))
        os.environ['AIOPS_FAST_WRITER_PROFILE'] = path
        os.environ['AIOPS_FAST_WRITER_PROFILE_SHA256'] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if args.profile is not None:
        from aiops_v4.data import sqlite_runtime
        path = str(args.profile.resolve(strict=True))
        os.environ['AIOPS_SQLITE_TUNING_PROFILE'] = path
        os.environ['AIOPS_SQLITE_TUNING_PROFILE_SHA256'] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        sqlite_runtime.install_from_environment()
    install_from_environment()
    sys.argv = [args.module, *remaining]
    runpy.run_module(args.module, run_name='__fast_writer_cli__', alter_sys=False)


def monitor_main():
    """Reuse the existing bounded read-only observer for registered live files."""
    import argparse
    import importlib.util
    parser = argparse.ArgumentParser()
    parser.add_argument('--fast-profile', type=Path, required=True)
    parser.add_argument('--monitor-source', type=Path, required=True)
    args, remaining = parser.parse_known_args(sys.argv[2:])
    value = json.loads(args.fast_profile.read_text())
    if value['implementation_sha256'] != _digest(Path(__file__)):
        raise ValueError('monitor fast implementation hash changed')
    writer = FastWriter(value['data_root'], value['fast_root'],
                        min_free_bytes=value['min_free_bytes'], max_fast_bytes=value['max_fast_bytes'])
    spec = importlib.util.spec_from_file_location('original_progress_monitor', args.monitor_source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bounded = module.bounded_sql
    def read(path, operation, warnings):
        try:
            actual = writer.monitored_path(path)
        except (OSError, ValueError) as error:
            warnings.append('Live writer metadata temporarily unavailable: ' + str(error))
            return None
        return bounded(actual, operation, warnings)
    module.bounded_sql = read
    sys.argv = [str(args.monitor_source), *remaining]
    raise SystemExit(module.main())


if __name__ in ('__main__', '__mp_main__'):
    sys.path.insert(0, str(REPO))
    if __name__ == '__mp_main__':
        if os.environ.get('AIOPS_SQLITE_TUNING_PROFILE'):
            from aiops_v4.data import sqlite_runtime
            sqlite_runtime.install_from_environment()
        install_from_environment()
    else:
        if len(sys.argv) > 1 and sys.argv[1] == 'monitor':
            monitor_main()
        else:
            main()


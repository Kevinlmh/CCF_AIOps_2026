"""Per-run disk ceilings and explicit worker memory budgets."""
from pathlib import Path
from contextlib import contextmanager
import os
import shutil
import stat
from time import monotonic


class WorkspaceBudgetError(RuntimeError):pass


class ResourcePolicy:
    def __init__(self,root,min_free_bytes=12*1024**3,max_workspace_bytes=100*1024**3,memory_bytes=4*1024**3,workers=4):
        for n,v in [('min_free_bytes',min_free_bytes),('max_workspace_bytes',max_workspace_bytes),('memory_bytes',memory_bytes)]:
            if type(v) is not int or v<(0 if n=='min_free_bytes' else 1):raise ValueError('invalid resource policy')
        if type(workers) is not int or not 1<=workers<=8 or memory_bytes//(2*workers)<128*1024**2:raise ValueError('invalid worker/memory budget')
        self.root=Path(root);self.min_free_bytes=min_free_bytes;self.max_workspace_bytes=max_workspace_bytes
        self.memory_bytes=memory_bytes;self.workers=workers;self.peak_bytes=0
        self._configured=False

    def configure_sqlite(self):
        import sqlite3
        temp=(self.root/'.sqlite-temp').resolve();temp.mkdir(exist_ok=True)
        with sqlite3.connect(':memory:') as db:
            db.execute("PRAGMA temp_store_directory='"+str(temp).replace("'","''")+"'")
            got=db.execute('PRAGMA temp_store_directory').fetchone()
            if got is None or Path(got[0])!=temp:raise RuntimeError('SQLite cannot confine temporary storage')
        self._configured=True

    def deleted_temporary_bytes(self):
        """Linux SQLite may unlink an open sort file; count owned process FDs."""
        import sys,multiprocessing
        if not sys.platform.startswith('linux'):return 0
        parent=multiprocessing.parent_process();owner=parent.pid if parent else os.getpid()
        pending=[owner];seen=set();files={};root=str(self.root.resolve())+'/'
        while pending:
            pid=pending.pop()
            if pid in seen:continue
            seen.add(pid);proc=Path('/proc')/str(pid)
            try:
                if proc.stat().st_uid!=os.getuid():continue
                pending.extend(map(int,(proc/'task'/str(pid)/'children').read_text().split()))
                for fd in (proc/'fd').iterdir():
                    try:
                        name=os.readlink(fd)
                        if name.startswith(root) and name.endswith(' (deleted)'):
                            s=fd.stat()
                            if stat.S_ISREG(s.st_mode):files[s.st_dev,s.st_ino]=s.st_blocks*512
                    except OSError:pass
            except (OSError,ValueError):continue
        return sum(files.values())

    @contextmanager
    def guard_sql(self,db):
        if not self._configured:self.configure_sqlite()
        self.check();failure=[];last=[0.]
        def monitor():
            now=monotonic()
            if now-last[0]<1:return 0
            last[0]=now
            try:self.check()
            except BaseException as error:failure.append(error);return 1
            return 0
        db.set_progress_handler(monitor,10000)
        try:
            yield
            self.check()
        except BaseException:
            if failure:raise failure[0]
            raise
        finally:db.set_progress_handler(None,0)

    def check(self):
        if not self._configured:self.configure_sqlite()
        used=0
        for path in self.root.rglob('*'):
            try:info=path.lstat()
            except FileNotFoundError:continue  # another worker committed/deleted its journal
            if stat.S_ISREG(info.st_mode):used+=info.st_blocks*512
        used+=self.deleted_temporary_bytes()
        import sys,resource,multiprocessing
        if sys.platform.startswith('linux'):
            pages=int(Path('/proc/self/statm').read_text().split()[1]);rss=pages*__import__('os').sysconf('SC_PAGE_SIZE')
        else:rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
        limit=self.memory_bytes//(2*self.workers) if multiprocessing.current_process().name!='MainProcess' else self.memory_bytes//2
        if rss>limit:raise WorkspaceBudgetError('memory budget exhausted; checkpoint retained')
        self.peak_bytes=max(self.peak_bytes,used)
        if used>self.max_workspace_bytes:raise WorkspaceBudgetError('workspace ceiling exhausted; checkpoint retained')
        if shutil.disk_usage(self.root).free<self.min_free_bytes:raise WorkspaceBudgetError('disk reserve exhausted; checkpoint retained')
        return used

    def worker_limit(self):
        self.configure_sqlite()
        import sys
        import multiprocessing
        if sys.platform.startswith('linux') and multiprocessing.current_process().name != 'MainProcess':
            import resource
            limit=self.memory_bytes//(2*self.workers)
            resource.setrlimit(resource.RLIMIT_AS,(limit,limit))

"""Exclusive producers preserve checkpoints while a writer or orphan worker lives."""
from contextlib import contextmanager
import fcntl,json,os,stat
from pathlib import Path

class ProducerBusyError(RuntimeError):
    pass


def process(pid):
    p=Path('/proc')/str(pid)
    try:
        f=(p/'stat').read_text().rsplit(')',1)[1].split()
        return dict(pid=int(pid),uid=p.stat().st_uid,ppid=int(f[1]),pgid=int(f[2]),sid=int(f[3]),start=f[19],state=f[0])
    except (OSError,ValueError,IndexError):return None


def workers(record):
    result=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        item=process(p.name)
        if not item or item['uid']!=record['uid'] or item['state'] in ('Z','X') or item['pid']==record['pid']:continue
        if item['pgid']!=record['pgid'] and item['sid']!=record['sid']:continue
        try:cmd=(p/'cmdline').read_bytes()
        except OSError:continue
        if b'multiprocessing.spawn' in cmd or b'multiprocessing.forkserver' in cmd:result.append(item['pid'])
    return result


def _validate(record):
    if not isinstance(record,dict) or set(record)!={'version','pid','uid','pgid','sid','start'} or record['version']!=1:
        raise ValueError('invalid producer identity')
    for key in ('pid','uid','pgid','sid'):
        if type(record[key]) is not int or record[key]<0:raise ValueError('invalid producer identity')
    if record['uid']!=os.getuid() or not isinstance(record['start'],str) or not record['start'].isdigit():raise ValueError('invalid producer identity')


def _lock_identity(fd,path):
    opened=os.fstat(fd);current=os.stat(path,follow_symlinks=False)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink!=1 or opened.st_uid!=os.getuid()
        or (opened.st_dev,opened.st_ino)!=(current.st_dev,current.st_ino)):
        raise ValueError('producer lock requires an unchanged regular single-link file')


@contextmanager
def exclusive_producer(directory):
    directory=Path(directory)
    if directory.is_symlink():raise ValueError('producer root symlink forbidden')
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'.producer.lock';flags=os.O_RDWR|os.O_CREAT|os.O_CLOEXEC|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(path,flags,0o600);locked=False;record=None
    try:
        _lock_identity(fd,path)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);locked=True
        except BlockingIOError:raise ProducerBusyError('producer already active; checkpoint retained') from None
        content=os.read(fd,4097)
        if len(content)>4096:raise ValueError('producer identity too large')
        if content:
            old=json.loads(content);_validate(old);owner=process(old['pid'])
            if owner and owner['uid']==old['uid'] and owner['start']==old['start'] and owner['state'] not in ('Z','X'):
                raise ProducerBusyError('producer owner still active; checkpoint retained')
            if workers(old):raise ProducerBusyError('producer workers still active; checkpoint retained')
        own=process(os.getpid())
        if own is None:raise RuntimeError('producer process identity unavailable')
        _lock_identity(fd,path)
        record={k:own[k] for k in ('pid','uid','pgid','sid','start')};record['version']=1
        os.lseek(fd,0,os.SEEK_SET);os.ftruncate(fd,0);os.write(fd,json.dumps(record).encode());os.fsync(fd)
        yield
    finally:
        try:
            if locked and record is not None and not workers(record):
                _lock_identity(fd,path)
                os.lseek(fd,0,os.SEEK_SET);os.ftruncate(fd,0);os.fsync(fd)
        finally:
            try:
                if locked:fcntl.flock(fd,fcntl.LOCK_UN)
            finally:os.close(fd)

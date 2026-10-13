import json,os,subprocess,sys
from pathlib import Path
import pytest
from aiops_v4.data.lock import exclusive_producer,ProducerBusyError

def test_reentry_refused_and_release(tmp_path):
    with exclusive_producer(tmp_path):
        with pytest.raises(ProducerBusyError):
            with exclusive_producer(tmp_path):pass
    with exclusive_producer(tmp_path):pass
    assert (tmp_path/'.producer.lock').read_text()==''

def test_exception_releases_when_no_workers(tmp_path):
    with pytest.raises(RuntimeError):
        with exclusive_producer(tmp_path):raise RuntimeError('stop')
    with exclusive_producer(tmp_path):pass

def test_symlink_lock_refused(tmp_path):
    target=tmp_path/'target';target.write_text('unchanged');(tmp_path/'.producer.lock').symlink_to(target)
    with pytest.raises((ValueError,OSError)):
        with exclusive_producer(tmp_path):pass
    assert target.read_text()=='unchanged'

def test_cross_process_busy(tmp_path):
    code='from aiops_v4.data.lock import exclusive_producer;import sys,time;from pathlib import Path;\nwith exclusive_producer(Path(sys.argv[1])): print("ready",flush=True);time.sleep(8)'
    p=subprocess.Popen([sys.executable,'-c',code,str(tmp_path)],stdout=subprocess.PIPE,text=True,start_new_session=True)
    try:
        assert p.stdout.readline().strip()=='ready'
        with pytest.raises(ProducerBusyError):
            with exclusive_producer(tmp_path):pass
    finally:p.terminate();p.wait(timeout=10)
    with exclusive_producer(tmp_path):pass


def test_orphan_spawn_worker_prevents_resume(tmp_path):
    import signal,time
    from aiops_v4.data.lock import process
    script=tmp_path/'orphan.py';info=tmp_path/'identity.json'
    script.write_text("from pathlib import Path;import os,json,time,multiprocessing;from aiops_v4.data.lock import process\nif __name__=='__main__':\n p=multiprocessing.get_context('spawn').Process(target=time.sleep,args=(30,));p.start();r=process(os.getpid());r={k:r[k] for k in ('pid','uid','pgid','sid','start')};r['version']=1;Path(__import__('sys').argv[1]).write_text(json.dumps(dict(owner=r,worker=p.pid)));os._exit(0)\n")
    run=subprocess.Popen([sys.executable,str(script),str(info)],start_new_session=True)
    try:
        run.wait(timeout=10);record=json.loads(info.read_text());worker=record['worker']
        # Wait for spawn's interpreter command to be visible.
        for _ in range(100):
            from aiops_v4.data.lock import workers
            if workers(record['owner']):break
            time.sleep(.02)
        assert workers(record['owner'])
        (tmp_path/'.producer.lock').write_text(json.dumps(record['owner']))
        with pytest.raises(ProducerBusyError):
            with exclusive_producer(tmp_path):pass
        assert json.loads((tmp_path/'.producer.lock').read_text())==record['owner']
    finally:
        try:os.killpg(run.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        for _ in range(100):
            if not workers(record['owner']):break
            time.sleep(.02)
    with exclusive_producer(tmp_path):pass

@pytest.mark.parametrize('child',[False,True])
def test_input_root_rejected_before_lock_or_directory_creation(tmp_path,child):
    from aiops_v4.data.pipeline import build_data_layers
    raw=tmp_path/'raw';raw.mkdir();source=raw/'source.csv';source.write_text('original')
    target=raw/'new-child' if child else raw
    with pytest.raises(ValueError,match='outside raw input'):
        build_data_layers({'raw_root':str(raw)},target)
    assert source.read_text()=='original'
    assert not (raw/'.producer.lock').exists()
    assert not (raw/'new-child').exists()


def test_hardlinked_lock_must_not_modify_original(tmp_path):
    raw=tmp_path/'raw.csv';out=tmp_path/'output';out.mkdir()
    original=json.dumps(dict(version=1,pid=0,uid=os.getuid(),pgid=0,sid=0,start='0'))
    raw.write_text(original);os.link(raw,out/'.producer.lock')
    with pytest.raises((ValueError,OSError)):
        with exclusive_producer(out):pass
    assert raw.read_text()==original


def test_nonregular_lock_refused(tmp_path):
    os.mkfifo(tmp_path/'.producer.lock')
    with pytest.raises((ValueError,OSError)):
        with exclusive_producer(tmp_path):pass


def test_changed_lock_inode_still_closes_descriptor(tmp_path):
    path=tmp_path/'.producer.lock';fd=None
    with pytest.raises(ValueError):
        with exclusive_producer(tmp_path):
            for item in Path('/proc/self/fd').iterdir():
                try:
                    if item.resolve()==path:fd=int(item.name)
                except FileNotFoundError:pass
            assert fd is not None
            path.rename(tmp_path/'moved.lock');path.touch()
    with pytest.raises(OSError):os.fstat(fd)

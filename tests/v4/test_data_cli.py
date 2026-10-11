import json
from pathlib import Path
import subprocess,sys
from test_records import write_csv


def test_data_build_verify_and_export_without_model(tmp_path):
    raw=tmp_path/'raw';write_csv(raw,'node_metrics.csv',[dict(timestamp=f'2026-09-17T04:{i:02d}:00Z',node='br1',cpu_usage='10') for i in range(15)])
    config=dict(schema_version=1,batch='stage2',raw_root=str(raw),naive_timezone='UTC',
        storage=dict(backend='canonical',workers=1,min_free_gib=0),agents=dict(discovery_only=True),backend=dict(kind='replay',model='offline',replay_file=str(tmp_path/'unused')))
    p=tmp_path/'config.json';p.write_text(json.dumps(config));out=tmp_path/'dataset'
    def run(*args):return subprocess.run([sys.executable,'-m','aiops_v4.data',*args],capture_output=True,text=True)
    result=run('build','--config',str(p),'--output-dir',str(out))
    assert result.returncode==0,result.stderr
    summary=json.loads(result.stdout.splitlines()[-1]);assert summary['stages']['features']['rows_scanned']==15
    verified=run('verify','--data-dir',str(out),'--raw');assert verified.returncode==0,verified.stderr
    exported=tmp_path/'windows.jsonl'
    command=run('export','--feature-store',summary['layers']['features']+'/features.sqlite','--output',str(exported))
    assert command.returncode==0,command.stderr
    assert len(exported.read_text().splitlines())==15

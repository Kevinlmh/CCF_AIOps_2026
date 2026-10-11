import importlib.util
import json
from pathlib import Path
import subprocess,sys
from test_records import write_csv


def test_benchmark_compares_real_legacy_and_canonical_artifacts_without_model(tmp_path):
    script=Path(__file__).resolve().parents[2]/'scripts/v4_data_benchmark.py'
    assert script.exists(), 'data benchmark not implemented'
    raw=tmp_path/'case';write_csv(raw,'node_metrics.csv',[dict(timestamp=f'2026-09-17T04:{i:02d}:00Z',node='br1',cpu_usage='90' if i in (12,13) else '10') for i in range(15)])
    out=tmp_path/'benchmark'
    result=subprocess.run([sys.executable,str(script),'--cases',str(raw),'--output-dir',str(out),'--repeats','1','--workers','1'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    report=json.loads((out/'benchmark.json').read_text())
    assert report['comparisons'][0]['equivalent']
    assert report['runs'][0]['backend_calls']==report['runs'][1]['backend_calls']==0
    assert report['runs'][0]['windows']==report['runs'][1]['windows']==15
    assert report['runs'][1]['final_bytes']>0

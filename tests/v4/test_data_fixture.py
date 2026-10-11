import csv
from pathlib import Path
import subprocess
import sys

from test_records import write_csv


def test_large_fixture_uses_exact_source_prefix_split_across_files(tmp_path):
    script=Path(__file__).resolve().parents[2]/'scripts/v4_make_data_fixture.py'
    source=write_csv(tmp_path/'source','netflow_5tuple_minute.csv',[
        dict(minute_utc='2026-09-17T04:00:00Z',node='br1',interface_id='eth0',protocol='6',packets=str(i),bytes=str(i*10))
        for i in range(25)])
    destination=tmp_path/'fixture'
    result=subprocess.run([sys.executable,str(script),'--netflow-file',str(source),
        '--output-dir',str(destination),'--netflow-rows','20'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    parts=sorted(destination.glob('netflow*.csv'))
    rows=[]
    for path in parts:
        with path.open(newline='') as stream:part=list(csv.DictReader(stream))
        assert len(part)==10
        rows+=part
    assert [r['packets'] for r in rows]==[str(i) for i in range(20)]
    assert len(list(destination.glob('node*.csv')))==2
    assert (destination/'fixture.json').exists()
    assert source.exists()

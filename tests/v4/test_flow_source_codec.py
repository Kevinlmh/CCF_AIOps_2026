from contextlib import closing
import sqlite3
from aiops_v4.features.netflow import NetflowAccumulator
from aiops_v4.features.source_stream import iter_netflow_windows
from test_flow_codec_optimization import row

def test_source_merge_reads_typed_and_legacy_flow_records(tmp_path):
    path=tmp_path/'source.sqlite'
    with closing(sqlite3.connect(path)) as db:
        flows=NetflowAccumulator(db,60,None,capacity=1)
        for index,value in enumerate([10.0,20.0,30.0],1):
            current=row(index,value=value)
            current['raw_values']['bytes']=str(value)
            flows.add(current)
        expected=list(flows.windows())
        assert db.execute('SELECT payload FROM flow_windows').fetchone()[0].startswith(b'V4F')
        db.commit()
    actual=list(iter_netflow_windows([(0,str(path))],60,None))
    assert actual==expected
    assert actual[0]['metrics']['bytes']['sum']==60.0
    assert actual[0]['record_count']==3

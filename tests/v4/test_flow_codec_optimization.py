"""Typed flow spill records preserve moments and evidence across bounded eviction."""
import copy
from datetime import datetime
from importlib import import_module, util
import sqlite3
from collections import OrderedDict

import pytest

from aiops_v4.features.netflow import NetflowAccumulator
from aiops_v4.storage import db_dump, load_json


def api():
    assert util.find_spec('aiops_v4.features.flow_codec'), 'typed NetFlow spill codec is missing'
    return import_module('aiops_v4.features.flow_codec')


def row(index=1, stamp='2026-09-17T04:00:00.000000Z', value=10.0):
    return dict(batch='stage2', series_key='flow-series', source='netflow', view='traffic',
                identity={'entity_id':'br1','city':'西安'}, dimensions={'interface_id':'ens4','protocol':'6'},
                timestamp=stamp, raw_values={'bytes':value,'packets':None}, flags=['missing_numeric:packets'],
                ref=dict(record_id=f'position:西安/netflow.csv:{index}',source_file='西安/netflow.csv',
                         record_index=index,line_start=index+1,line_end=index+1,byte_start=index*80,
                         byte_length=80,_cells=[stamp,'br1',str(value),'']))


def value():
    stamp='2026-09-17T04:00:00.000000Z'
    return dict(template=row(),count=3,flags=['custom_flag','missing_numeric:packets'],
                refs=[dict(time=stamp,ref=row()['ref'])],last_time_seen=stamp,
                metrics={'bytes':dict(inputs=3,missing=0,invalid=0,n=3,zeros=0,mean=20.0,m2=200.0,
                        total=60.0,min=10.0,max=30.0,first=[stamp,'西安/netflow.csv',1,10.0],
                        last=[stamp,'西安/netflow.csv',3,20.0]),
                    'packets':dict(inputs=3,missing=1,invalid=1,n=1,zeros=1,mean=-0.0,m2=None,
                        total=None,min=-0.0,max=-0.0,first=[stamp,'西安/netflow.csv',2,-0.0],
                        last=[stamp,'西安/netflow.csv',2,-0.0],custom_metric={'kept':True})},
                future_field={'missing':None,'large_integer':2**70})


def test_typed_flow_payload_roundtrips_bits_nulls_unknown_fields_and_template_variants():
    with sqlite3.connect(':memory:') as db:
        codec=api().FlowCodec(db,create=True)
        first=value();payload=codec.encode(first)
        assert payload.startswith(api().MAGIC) and not payload.startswith(b'V4Z1')
        assert codec.decode(payload)==first
        restored=codec.decode(payload)
        assert restored['metrics']['packets']['mean'].hex()=='-0x0.0p+0'
        assert restored['metrics']['packets']['min'].hex()=='-0x0.0p+0'
        changed=copy.deepcopy(first)
        changed['template']['timestamp']='2026-09-17T04:01:00.000000Z'
        changed['template']['raw_values']={'bytes':'invalid','packets':''}
        changed['template']['identity']['city']='other city'
        changed['template']['flags']=[]
        changed['template']['ref']['source_file']='other/file.csv'
        changed['template']['future_template']={'retained':'yes'}
        changed['refs'][0]['ref']['custom_anchor']=None
        changed['metrics']['new_metric']=copy.deepcopy(changed['metrics']['bytes'])
        changed.pop('last_time_seen')
        second=codec.encode(changed)
        assert codec.decode(second)==changed
        assert db.execute('SELECT count(*) FROM flow_templates').fetchone()[0]==1
        fresh=api().FlowCodec(db)
        assert fresh.decode(payload)==first and fresh.decode(second)==changed
        assert fresh.encode(first)==payload


def test_legacy_flow_rows_and_checkpoint_layout_remain_usable():
    with sqlite3.connect(':memory:') as db:
        codec=api().FlowCodec(db)
        original=value();payload=db_dump(original)
        assert codec.decode(payload)==original
        assert codec.encode(original)==payload
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='flow_templates'").fetchone()
        compact=api().FlowCodec(db,create=True)
        assert compact.decode(payload)==original


def test_truncated_or_corrupted_flow_payload_is_rejected():
    with sqlite3.connect(':memory:') as db:
        codec=api().FlowCodec(db,create=True);payload=codec.encode(value())
        for broken in (payload[:4],payload[:-1],payload+b'extra',api().MAGIC+b'\xff'*12):
            with pytest.raises(ValueError):codec.decode(broken)


def test_repeated_flow_timestamp_parses_only_when_timestamp_changes(monkeypatch):
    import aiops_v4.features.netflow as netflow
    calls=[]
    class CountingDateTime:
        @staticmethod
        def fromisoformat(stamp):
            calls.append(stamp);return datetime.fromisoformat(stamp)
    monkeypatch.setattr(netflow,'datetime',CountingDateTime)
    with sqlite3.connect(':memory:') as db:
        accumulator=NetflowAccumulator(db,60,None,capacity=1)
        for current in [row(1),row(2,value=30.0),row(3,'2026-09-17T04:01:00.000000Z',1.0),row(4,value=20.0)]:
            current['raw_values']['bytes']=str(current['raw_values']['bytes'])
            accumulator.add(current)
        assert len(calls)==3, 'identical adjacent timestamps are still reparsed'
        windows=list(accumulator.windows())
        assert windows[0]['record_count']==3
        assert windows[0]['metrics']['bytes']['mean']==20.0
        assert windows[0]['metrics']['bytes']['sum']==60.0
        assert windows[0]['coverage']['observed_timestamps']==1
        payloads=[payload for payload, in db.execute('SELECT payload FROM flow_windows')]
        assert all(payload.startswith(api().MAGIC) for payload in payloads)


def test_legacy_resumed_accumulator_uses_json_spill_without_losing_old_values():
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE flow_windows(series TEXT,start TEXT,payload BLOB,PRIMARY KEY(series,start)) WITHOUT ROWID')
        db.execute('CREATE TABLE flow_times(series TEXT,start TEXT,time TEXT,PRIMARY KEY(series,start,time)) WITHOUT ROWID')
        saved=value();saved.pop('future_field');saved['metrics'].pop('packets');saved['flags']=[]
        db.execute('INSERT INTO flow_windows VALUES (?,?,?)',('flow-series','2026-09-17T04:00:00.000000Z',db_dump(saved)))
        db.execute('INSERT INTO flow_times VALUES (?,?,?)',('flow-series','2026-09-17T04:00:00.000000Z',saved['last_time_seen']))
        accumulator=NetflowAccumulator.__new__(NetflowAccumulator)
        accumulator.db=db;accumulator.width=60;accumulator.step=None;accumulator.capacity=1;accumulator.cache=OrderedDict()
        current=row(4,value=40.0);current['raw_values']['bytes']='40.0'
        accumulator.add(current);windows=list(accumulator.windows())
        assert windows[0]['record_count']==4 and windows[0]['metrics']['bytes']['sum']==100.0
        assert windows[0]['metrics']['bytes']['mean']==25.0
        assert db.execute('SELECT payload FROM flow_windows').fetchone()[0].startswith(b'V4Z1')


def test_flow_template_cache_is_bounded_by_retained_python_bytes():
    with sqlite3.connect(':memory:') as db:
        codec=api().FlowCodec(db,create=True,cache_bytes=4096)
        for index in range(30):
            current=value();current['template']['series_key']=f'series-{index}'
            current['template']['identity']['label']='x'*2000
            payload=codec.encode(current)
            assert codec.decode(payload)==current
            assert codec.cache_bytes<=4096
        assert db.execute('SELECT count(*) FROM flow_templates').fetchone()[0]==30


def test_fast_flow_parser_reuses_bucket_before_datetime_parse(monkeypatch):
    from aiops_v4.features.spool import FastFlows
    import aiops_v4.features.spool as spool
    import aiops_v4.features.netflow as netflow
    calls=[]
    class CountingDateTime:
        @staticmethod
        def fromisoformat(stamp):
            calls.append(stamp);return datetime.fromisoformat(stamp)
    monkeypatch.setattr(spool,'datetime',CountingDateTime)
    monkeypatch.setattr(netflow,'datetime',CountingDateTime)
    with sqlite3.connect(':memory:') as db:
        accumulator=FastFlows(db,60,None,capacity=1)
        for current in [row(1),row(2,value=30.0),row(3,'2026-09-17T04:01:00.000000Z',1.0)]:
            accumulator.add(current)
        assert len(calls)==2, 'FastFlows reparses identical timestamps before its bucket guard'
        accumulator.flush()
        assert all(payload.startswith(api().MAGIC) for payload, in db.execute('SELECT payload FROM flow_windows'))


def test_template_patch_preserves_signed_zero_and_numeric_type_changes():
    with sqlite3.connect(':memory:') as db:
        codec=api().FlowCodec(db,create=True)
        original=value();original['template']['raw_values']['bytes']=0.0
        codec.encode(original)
        changed=copy.deepcopy(original);changed['template']['raw_values']['bytes']=-0.0
        restored=codec.decode(codec.encode(changed))
        assert restored['template']['raw_values']['bytes'].hex()=='-0x0.0p+0'
        changed['template']['raw_values']['bytes']=0
        restored=codec.decode(codec.encode(changed))
        assert type(restored['template']['raw_values']['bytes']) is int


def test_flow_spill_is_smaller_than_json_for_eight_retained_csv_anchors():
    current=value();stamp=current['last_time_seen']
    current['refs']=[dict(time=stamp,ref=row(index+1,stamp,float(index))['ref']) for index in range(8)]
    for index,reference in enumerate(current['refs']):
        reference['ref']['_cells']=[stamp,'xian-br-001','ens4','6','xian','1.2.3.4','target-id',
                                   str(index),str(index*100),'86400','normal','西安']
    with sqlite3.connect(':memory:') as db:
        codec=api().FlowCodec(db,create=True)
        payload=codec.encode(current)
        assert codec.decode(payload)==current
        assert len(payload)<len(db_dump(current))*.9, 'typed spill repeats strings that JSON compressed'


def test_typed_flow_decoder_rejects_nonprimitive_anchor_and_nonfinite_point():
    import marshal
    import zlib
    with sqlite3.connect(':memory:') as db:
        codec=api().FlowCodec(db,create=True);payload=codec.encode(value())
        body=marshal.loads(zlib.decompress(payload[len(api().MAGIC):]))
        if isinstance(body[5][0],dict):body[5][0]['ref']['_cells']=compile('1+2','corrupt','eval')
        elif len(body[5][0])==8:body[5][0][5]=compile('1+2','corrupt','eval')
        else:body[5][0][2][-1]=compile('1+2','corrupt','eval')
        with pytest.raises(ValueError):
            codec.decode(api().MAGIC+zlib.compress(marshal.dumps(body,2),1))
        body=marshal.loads(zlib.decompress(payload[len(api().MAGIC):]))
        body[9][0][10][-1]=float('nan')
        with pytest.raises(ValueError):
            codec.decode(api().MAGIC+zlib.compress(marshal.dumps(body,2),1))


def test_flow_cache_obeys_byte_budget_and_reports_eviction_reload_without_changed_moments():
    with sqlite3.connect(':memory:') as db:
        accumulator=NetflowAccumulator(db,60,None,capacity=3,cache_budget_bytes=15000)
        for index,(series,number) in enumerate([('a',10.0),('b',1.0),('c',2.0),('a',30.0)],1):
            current=row(index,value=number);current['series_key']=series
            current['raw_values']['bytes']=str(number)
            accumulator.add(current)
            assert accumulator.cache_bytes<=15000
            assert len(accumulator.cache)<=3
        stats=accumulator.cache_report()
        assert stats['evictions']>0 and stats['reloads']>0
        assert stats['spill_writes']>0 and stats['encoded_bytes']>0
        windows=list(accumulator.windows())
        a=next(window for window in windows if window['record_count']==2)
        assert a['metrics']['bytes']['sum']==40.0 and a['metrics']['bytes']['mean']==20.0
        assert [ref['record_index'] for ref in a['references']]==[1,4]
        assert accumulator.cache_bytes==0


@pytest.mark.parametrize('location',['window','metric','reference'])
def test_typed_flow_decoder_rejects_extras_overriding_reserved_fields(location):
    import marshal
    import zlib
    with sqlite3.connect(':memory:') as db:
        codec=api().FlowCodec(db,create=True);payload=codec.encode(value())
        body=marshal.loads(zlib.decompress(payload[len(api().MAGIC):]))
        if location=='window':body[10]={'count':None}
        elif location=='metric':body[9][0][-1]={'n':None}
        else:body[5][0][6]={'source_file':None}
        with pytest.raises(ValueError):
            codec.decode(api().MAGIC+zlib.compress(marshal.dumps(body,2),1))


def test_parse_file_flow_cache_uses_global_worker_share_and_checkpoint_scope(tmp_path):
    from aiops_v4.data.discovery import discover_sources
    from aiops_v4.features.spool import parse_file
    from test_records import write_csv
    raw=tmp_path/'raw'
    write_csv(raw,'netflow.csv',[dict(minute_utc='2026-09-17T04:00:00Z',node_key='br1',
                                   interface_id='ens4',protocol='6',bytes=str(number)) for number in (10,30,20)])
    file=discover_sources(raw)[0];path=tmp_path/'flow.sqlite'
    settings=dict(workspace_root=str(tmp_path),min_free_bytes=0,max_workspace_bytes=1024**3,
                  memory_bytes=1024**3,workers=4,max_rows_per_file=None,naive_timezone='UTC',
                  netflow_streaming=True,window_seconds=60,expected_step_seconds=None,
                  counter_max_gap_seconds=180,checkpoint_rows=1,checkpoint_seconds=3600)
    def stop(progress):
        if progress.get('event')=='parse_checkpoint':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):parse_file((file,'stage2',path,settings),stop)
    result=parse_file((file,'stage2',path,settings))
    stats=result['netflow_cache']
    assert stats['budget_bytes']==32*1024**2
    assert stats['peak_bytes']<=stats['budget_bytes']
    assert stats['capacity']<=65536
    assert stats['scope']=='process_since_resume'
    assert result['rows_scanned']==3 and result['essential_profile']['accepted']==3
    with sqlite3.connect(path) as db:
        current=api().FlowCodec(db).decode(db.execute('SELECT payload FROM flow_windows').fetchone()[0])
        assert current['count']==3 and current['metrics']['bytes']['mean']==20.0
        assert current['metrics']['bytes']['m2']==200.0 and current['metrics']['bytes']['total']==60.0


@pytest.mark.parametrize('payload',[
    b'['+(100000).to_bytes(4,'little',signed=True),
    b'('+(100000).to_bytes(4,'little',signed=True),
    b'u'+(100000).to_bytes(4,'little',signed=True),
    b's'+(100000).to_bytes(4,'little',signed=True),
    b'l'+(100000).to_bytes(4,'little',signed=True),
    b'['+(-1).to_bytes(4,'little',signed=True),
    b'c',b'r',b'\xdb\x01\x00\x00\x00N',
    (b'[\x01\x00\x00\x00'*70)+b'N',
])
def test_invalid_marshal_declarations_are_rejected_before_native_allocation(monkeypatch,payload):
    codec=api()
    def native_loader_was_reached(_):
        pytest.fail('native marshal reader was called before allocation headers were bounded')
    monkeypatch.setattr(codec.marshal,'loads',native_loader_was_reached)
    with pytest.raises(ValueError):codec._unbinary(payload)


def test_truncated_marshal_container_has_small_peak_allocation():
    import tracemalloc
    codec=api();payload=b'['+(100000).to_bytes(4,'little',signed=True)
    tracemalloc.start()
    try:
        with pytest.raises(ValueError):codec._unbinary(payload)
        _,peak=tracemalloc.get_traced_memory()
    finally:tracemalloc.stop()
    assert peak<64*1024, 'tiny corrupt header allocated its declared native list'


def test_complete_marshal_container_exceeding_node_budget_never_reaches_native_reader(monkeypatch):
    codec=api();payload=codec.marshal.dumps([None]*8,2)
    monkeypatch.setattr(codec,'MAX_TYPED_NODES',8,raising=False)
    def native_loader_was_reached(_):
        pytest.fail('native marshal reader was called for an excessive complete container')
    monkeypatch.setattr(codec.marshal,'loads',native_loader_was_reached)
    with pytest.raises(ValueError):codec._unbinary(payload)

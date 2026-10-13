"""Bounded ingest retains CSV evidence and commits complete logical prefixes."""
import hashlib
from importlib import import_module, util
import sqlite3

import pytest

from aiops_v4.data.discovery import discover_sources
from aiops_v4.data.essential import EssentialProfile
from aiops_v4.data.position import PositionReader, PositionedRow, record_at
from aiops_v4.features.series import prepare_record
from aiops_v4.features.spool import PreparedParser, parse_file
from aiops_v4.features.spool_codec import ObservationCodec
from aiops_v4.storage import load_json
from test_records import write_csv


def api():
    assert util.find_spec('aiops_v4.features.ingest_batch'), 'bounded ingest batching is missing'
    return import_module('aiops_v4.features.ingest_batch')


def settings(root, **changes):
    values = dict(workspace_root=str(root), min_free_bytes=0, max_workspace_bytes=1024**3,
                  memory_bytes=1024**3, workers=1, max_rows_per_file=None, naive_timezone='UTC',
                  netflow_streaming=False, window_seconds=60, expected_step_seconds=None,
                  counter_max_gap_seconds=180, checkpoint_rows=100000, checkpoint_seconds=3600)
    values.update(changes)
    return values


def decoded(path):
    with sqlite3.connect(path) as db:
        codec = ObservationCodec(db)
        return [codec.decode(*row) for row in db.execute('SELECT series,time,idx,payload FROM observations ORDER BY idx')]


def test_batch_flushes_at_row_and_byte_bound_without_committing(tmp_path):
    batch_type = api().BoundedSQLBatch
    path = tmp_path/'batch.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE cells(idx INTEGER,payload BLOB)')
        db.commit()
        batch = batch_type(db, 'INSERT INTO cells VALUES (?,?)', max_rows=2, max_bytes=512)
        batch.add((1,b'a'))
        assert db.execute('SELECT count(*) FROM cells').fetchone()[0] == 0
        batch.add((2,b'b'))
        assert db.execute('SELECT idx FROM cells ORDER BY idx').fetchall() == [(1,),(2,)]
        assert batch.pending_rows == batch.pending_bytes == 0
        assert db.in_transaction
        batch.add((3,b'x'*10000))
        assert batch.pending_rows == batch.pending_bytes == 0
        assert db.execute('SELECT count(*) FROM cells').fetchone()[0] == 3
        assert batch.peak_pending_rows <= 2 and batch.peak_pending_bytes <= 512
        db.rollback()
        assert db.execute('SELECT count(*) FROM cells').fetchone()[0] == 0


def test_batch_limits_flush_before_oversized_record_and_retain_order(tmp_path):
    batch_type = api().BoundedSQLBatch
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE cells(idx INTEGER,payload BLOB)')
        batch = batch_type(db, 'INSERT INTO cells VALUES (?,?)', max_rows=4096, max_bytes=1024)
        for index in range(100):
            batch.add((index,b'x'*180 if index != 21 else b'x'*10000))
        batch.flush()
        assert db.execute('SELECT idx FROM cells').fetchall() == [(i,) for i in range(100)]
        assert batch.peak_pending_bytes <= 1024
        assert batch.peak_pending_rows <= 4096
        assert batch.batches > 1


def test_parse_spool_inserts_observations_via_bounded_executemany(tmp_path, monkeypatch):
    raw=tmp_path/'raw'
    write_csv(raw,'node_metrics.csv',[dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage=str(i)) for i in range(4100)])
    file=discover_sources(raw)[0]
    original_connect=sqlite3.connect
    batches=[]
    class Tracking(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            assert not sql.startswith('INSERT INTO observations VALUES'), 'per-row observation INSERT remains'
            return super().execute(sql,parameters)
        def executemany(self, sql, parameters):
            if sql.startswith('INSERT INTO observations VALUES'):
                parameters=list(parameters);batches.append(len(parameters))
            return super().executemany(sql,parameters)
    def connect(*args,**kwargs):
        kwargs['factory']=Tracking
        return original_connect(*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',connect)
    path=tmp_path/'spool.sqlite'
    manifest=parse_file((file,'stage2',path,settings(tmp_path)))
    assert manifest['rows_scanned']==4100 and len(decoded(path))==4100
    assert len(batches)==2 and batches[0]==4096 and batches[1]==4


def test_netflow_parse_plan_reads_compiled_positions_without_full_raw_dict(tmp_path,monkeypatch):
    raw=tmp_path/'raw'
    write_csv(raw,'netflow.csv',[dict(minute_utc='2026-09-17T04:00:00Z',node='br1',interface_id='ens4',protocol='6',bytes='-0.0',packets='bad')])
    file=discover_sources(raw)[0]
    def materialized_raw(*_args):
        pytest.fail('streaming NetFlow materialized all columns as a raw dict')
    monkeypatch.setattr(PositionedRow,'raw',materialized_raw)
    with PositionReader(file,'stage2') as reader:
        row=PreparedParser(reader,'UTC',True).parse(next(iter(reader)))
    assert row['raw_values']['bytes']==-0.0
    assert row['raw_values']['packets']=='bad'
    assert row['flags']==['invalid_numeric:packets']


@pytest.mark.parametrize('name,rows', [
 ('node_metrics.csv', [dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage='-0.0',load1='NA'),
                       dict(timestamp='invalid',node='br1',cpu_usage='bad',load1='Infinity'),
                       dict(timestamp='2026-09-17T12:00:00',node='aux-中文',cpu_usage='1.0000000000000001',load1='')]),
 ('routing_metrics.csv', [dict(timestamp='2026-09-17T04:00:00Z',node='br1',metric_name='peer_total',label='peer="西安",label="collision"',value='1e308'),
                          dict(timestamp='2026-09-17T04:00:00Z',node='br1',metric_name='peer_total',label='peer="西安",label="collision"',value='1e309')]),
 ('traffic_flow_metrics.csv', [dict(timestamp_utc='bad',prometheus_sample_time_utc='2026-09-17T04:00:00Z',source_region='xian',flow_type='web',series_key='a',web_flow_bytes_total='100',dns_flow_qps='bad'),
                               dict(timestamp_utc='2026-09-17T12:00:00',prometheus_sample_time_utc='',source_region='none',flow_type='dns',series_key='a',web_flow_bytes_total='bad',dns_flow_qps='-0.0')]),
 ('interface_metrics.csv', [dict(timestamp='2026-09-17T04:00:00Z',node_key='br1',region_code='xian',interface_id='ens4',rx_bytes_rate='-0.0',bad='not numeric'),
                              dict(timestamp='2026-09-17T04:01:00Z',node_key='br1',region_code='unknown',interface_id='NA',rx_bytes_rate='1e309',bad='NULL')]),
 ('scrape_health.csv', [dict(timestamp='2026-09-17T04:00:00Z',hostname='aux-service',source_region='xian',target_id='srv',exporter_type='custom',scrape_up='0',scrape_duration_seconds='bad'),
                        dict(timestamp='2026-09-17T04:01:00Z',hostname='aux-service',source_region='xian',target_id='',exporter_type='custom',scrape_up='NA',scrape_duration_seconds='Infinity')]),
 ('netflow.csv', [dict(minute_utc='bad',first_seen='2026-09-17T04:00:00Z',hostname='aux-router',source_region='xian',source_ip='1.2.3.4',interface_id='eth0',protocol='6',bytes='-0.0',packets='bad'),
                  dict(minute_utc='2026-09-17T12:00:00',first_seen='bad',hostname='aux-router',source_region='xian',source_ip='1.2.3.4',interface_id='NULL',protocol='',bytes='Infinity',packets='NA')]),
 ('frr_syslog_events.csv',[dict(event_time='bad',received_at='2026-09-17T04:00:00Z',hostname='br1',program='bgpd',severity='INFO',message='第一行\n第二行'+('原文'*800000))]),
])
def test_compiled_parser_matches_original_validator_and_original_raw_ids(tmp_path,name,rows):
    raw=tmp_path/'raw';write_csv(raw,name,rows)
    file=discover_sources(raw)[0]
    with PositionReader(file,'stage2') as reader:
        positions=list(reader);manifest=reader.manifest()
        expected=[];profile=EssentialProfile()
        for position in positions:
            try:row=prepare_record(position.record(reader),'Asia/Shanghai')
            except ValueError:profile.rejected+=1
            else:
                row['ref'].update(byte_start=position.byte_start,byte_length=position.byte_length)
                expected.append(row);profile.add(row)
    path=tmp_path/'spool.sqlite'
    result=parse_file((file,'stage2',path,settings(tmp_path,naive_timezone='Asia/Shanghai')))
    assert decoded(path)==expected
    assert result['essential_profile']==profile.report()
    assert result['raw_bytes_sha256']==hashlib.sha256(file.path.read_bytes()).hexdigest()
    assert result['identity']==manifest['identity']
    for row in decoded(path):
        assert record_at(raw,'stage2',result,row['ref']).record_id==row['ref']['record_id']


@pytest.mark.parametrize('newline',[b'\n',b'\r\n',b'\r'])
def test_batch_checkpoint_is_complete_prefix_with_crossline_unicode_short_extra_and_rejected(tmp_path,newline):
    raw=tmp_path/'raw';raw.mkdir();source=raw/'frr_syslog_events.csv'
    source.write_bytes(newline.join([b'\xef\xbb\xbf',b'event_time,hostname,message',
        '2026-09-17T04:00:00Z,br1,"首行'.encode(),'第二行"'.encode(),b'bad,br1,reject',
        b'2026-09-17T04:01:00Z,br1',b'',b'2026-09-17T04:02:00Z,br1,text,extra',b'']))
    file=discover_sources(raw)[0];path=tmp_path/'spool.sqlite'
    def stop(event):
        if event['event']=='parse_checkpoint':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):parse_file((file,'stage2',path,settings(tmp_path,checkpoint_rows=3)),stop)
    with sqlite3.connect(path) as db:
        saved=load_json(db.execute("SELECT payload FROM metadata WHERE key='checkpoint'").fetchone()[0])
        assert db.execute('SELECT idx FROM observations ORDER BY idx').fetchall()==[(1,),(3,)]
    assert saved['reader']['record_index']==3
    prefix=source.read_bytes()[:saved['reader']['byte_offset']]
    assert saved['reader']['prefix_sha256']==hashlib.sha256(prefix).hexdigest()
    result=parse_file((file,'stage2',path,settings(tmp_path,checkpoint_rows=3)))
    assert [r['ref']['record_index'] for r in decoded(path)]==[1,3,4]
    assert result['essential_profile']['accepted']==3 and result['essential_profile']['rejected']==1
    assert result['essential_profile']['flags']=={'short_csv_row':1,'extra_csv_cells':1,'unmapped_entity':3}


def test_checkpoint_before_first_batch_and_csv_error_roll_back_all_rows(tmp_path):
    raw=tmp_path/'raw';raw.mkdir();source=raw/'node_metrics.csv'
    source.write_text('timestamp,node,cpu_usage\n2026-09-17T04:00:00Z,br1,1\n"unclosed,br1,2\n')
    file=discover_sources(raw)[0];path=tmp_path/'spool.sqlite'
    with pytest.raises(ValueError,match='invalid CSV'):parse_file((file,'stage2',path,settings(tmp_path)))
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==0
        assert not db.execute("SELECT payload FROM metadata WHERE key='checkpoint'").fetchone()
        assert not db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()


def test_general_compiled_parser_reuses_column_slots_and_keeps_returned_rows_independent(tmp_path,monkeypatch):
    raw=tmp_path/'raw'
    write_csv(raw,'node_metrics.csv',[dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage='1'),
                                     dict(timestamp='2026-09-17T04:01:00Z',node='br1',cpu_usage='2')])
    file=discover_sources(raw)[0]
    def rebuilt_raw(*_args):
        pytest.fail('general parser rebuilt the full raw dictionary')
    monkeypatch.setattr(PositionedRow,'raw',rebuilt_raw)
    with PositionReader(file,'stage2') as reader:
        parser=PreparedParser(reader,'UTC',False)
        rows=[parser.parse(p) for p in reader]
    assert [r['raw_values'] for r in rows]==[{'cpu_usage':'1'},{'cpu_usage':'2'}]
    assert rows[0]['timestamp']=='2026-09-17T04:00:00.000000Z'
    assert rows[0]['ref']['record_id'] != rows[1]['ref']['record_id']


def test_parser_memo_never_retains_a_single_oversized_key_or_unbounded_grains():
    memo=api().BoundedMemo(max_rows=2,max_bytes=1024)
    first=memo.get_or_create('a',lambda:'small')
    assert memo.get_or_create('a',lambda:pytest.fail('memo missed retained value'))==first
    memo.get_or_create('oversized',lambda:'x'*10000)
    assert 'oversized' not in memo.values
    for index in range(100):memo.get_or_create(str(index),lambda:('x'*80,))
    assert len(memo.values)<=2 and memo.bytes<=1024


def test_streaming_flow_times_uses_batch_and_checkpoint_contains_every_time(tmp_path,monkeypatch):
    raw=tmp_path/'raw'
    write_csv(raw,'netflow.csv',[dict(minute_utc=f'2026-09-17T04:00:{i:02d}Z',node='br1',interface_id='ens4',protocol='6',bytes=str(i)) for i in range(5)])
    file=discover_sources(raw)[0]
    original_connect=sqlite3.connect;batches=[]
    class Tracking(sqlite3.Connection):
        def execute(self,sql,parameters=()):
            assert not sql.startswith('INSERT OR IGNORE INTO flow_times'), 'per-row flow time INSERT remains'
            return super().execute(sql,parameters)
        def executemany(self,sql,parameters):
            if sql.startswith('INSERT OR IGNORE INTO flow_times'):
                parameters=list(parameters);batches.append(len(parameters))
            return super().executemany(sql,parameters)
    def connect(*args,**kwargs):
        kwargs['factory']=Tracking
        return original_connect(*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',connect)
    path=tmp_path/'flow.sqlite'
    def stop(event):
        if event['event']=='parse_checkpoint':raise KeyboardInterrupt()
    options=settings(tmp_path,netflow_streaming=True,checkpoint_rows=3)
    with pytest.raises(KeyboardInterrupt):parse_file((file,'stage2',path,options),stop)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM flow_times').fetchone()[0]==3
    result=parse_file((file,'stage2',path,options))
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM flow_times').fetchone()[0]==5
    assert batches==[3,2]
    assert result['essential_profile']['accepted']==5


def test_general_validator_reuses_repeated_region_node_and_dimensions(tmp_path,monkeypatch):
    from aiops_v4.data import reader as reader_module
    raw=tmp_path/'raw'
    write_csv(raw,'routing_metrics.csv',[dict(timestamp=f'2026-09-17T04:00:{i:02d}Z',node='br1',region='xian',metric_name='peer_total',label='peer="西安"',value=str(i)) for i in range(10)])
    file=discover_sources(raw)[0];calls=[];original=reader_module._node
    def count_node(*args):
        calls.append(args);return original(*args)
    monkeypatch.setattr(reader_module,'_node',count_node)
    with PositionReader(file,'stage2') as reader:
        parser=PreparedParser(reader,'UTC',False);rows=[parser.parse(p) for p in reader]
    assert len(rows)==10
    assert len(calls)==2, 'each row reparsed its repeated region/node/label grain'
    assert all(row['dimensions']['peer']=='西安' for row in rows)


def test_malformed_csv_after_automatic_batch_flush_keeps_only_committed_prefix(tmp_path):
    raw=tmp_path/'raw';raw.mkdir();source=raw/'node_metrics.csv'
    source.write_text('timestamp,node,cpu_usage\n'+
                      ''.join('2026-09-17T04:00:00Z,br1,%d\n'%i for i in range(4100))+
                      '"unclosed,br1,4100\n')
    file=discover_sources(raw)[0]
    for prefix in (None,2):
        path=tmp_path/('first.sqlite' if prefix is None else 'checkpoint.sqlite')
        checkpoint_rows=100000 if prefix is None else 2
        # A single durable prefix is enough to prove rollback of a later full
        # automatic SQL batch. Extend the subsequent checkpoint deadline.
        options=settings(tmp_path,checkpoint_rows=checkpoint_rows)
        def later_checkpoint(event):
            if event['event']=='parse_checkpoint':options['checkpoint_rows']=100000
        with pytest.raises(ValueError,match='invalid CSV'):
            parse_file((file,'stage2',path,options),later_checkpoint)
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==(prefix or 0)
            assert not db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()
            saved=db.execute("SELECT payload FROM metadata WHERE key='checkpoint'").fetchone()
            assert (load_json(saved[0])['reader']['record_index'] if saved else None)==prefix

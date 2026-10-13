"""Compact private spools retain the complete prepared-record contract."""
import copy
from importlib import import_module, util
import sqlite3

import pytest

from aiops_v4.data.discovery import discover_sources
from aiops_v4.data.essential import EssentialProfile
from aiops_v4.data.position import PositionReader, record_at
from aiops_v4.features.aggregate import iter_windows
from aiops_v4.features.spool import PreparedParser, parse_file
from aiops_v4.storage import db_dump, load_json
from test_records import write_csv


def api():
    assert util.find_spec('aiops_v4.features.spool_codec'), 'compact observation codec is missing'
    return import_module('aiops_v4.features.spool_codec')


def settings(root, **extra):
    return dict(workspace_root=str(root), min_free_bytes=0, max_workspace_bytes=1024**3,
                memory_bytes=1024**3, workers=1, max_rows_per_file=None, naive_timezone='UTC',
                netflow_streaming=False, window_seconds=60, expected_step_seconds=None,
                counter_max_gap_seconds=180, checkpoint_rows=2, checkpoint_seconds=3600, **extra)


def prepared(file):
    with PositionReader(file, 'stage2') as reader:
        parser = PreparedParser(reader, 'UTC', False)
        rows = []
        profile = EssentialProfile()
        for position in reader:
            try:
                row = parser.parse(position)
            except ValueError:
                profile.rejected += 1
            else:
                rows.append(row)
                profile.add(row)
        manifest = reader.manifest()
    return rows, manifest, profile.report()


def decoded(db):
    codec = api().ObservationCodec(db)
    rows = [codec.decode(*row) for row in db.execute('SELECT series,time,idx,payload FROM observations')]
    return sorted(rows, key=lambda r:(r['series_key'],r['timestamp'],r['ref']['record_index']))


def test_parse_file_uses_integer_grain_and_smaller_private_spool(tmp_path):
    raw = tmp_path/'raw'
    rows = [dict(timestamp=f'2026-09-17T04:{i//60:02d}:{i%60:02d}Z',node='br1',
                 metric_name='peer_total',label='peer="2001:db8::1",description="西安上联"',value=str(i))
            for i in range(600)]
    write_csv(raw, 'routing_metrics.csv', rows)
    file = discover_sources(raw)[0]
    expected, _, _ = prepared(file)
    old_path = tmp_path/'legacy.sqlite'
    with sqlite3.connect(old_path) as old:
        old.execute('CREATE TABLE observations(series TEXT,time TEXT,idx INTEGER,payload BLOB)')
        old.executemany('INSERT INTO observations VALUES (?,?,?,?)',
                        [(r['series_key'],r['timestamp'],r['ref']['record_index'],db_dump(r)) for r in expected])
        old.execute('CREATE INDEX observations_order ON observations(series,time,idx)')
    path = tmp_path/'compact.sqlite'
    parse_file((file,'stage2',path,settings(tmp_path)))
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT DISTINCT typeof(series) FROM observations').fetchall() == [('integer',)]
        assert decoded(db) == expected
        assert len(api().series_keys(db)) == 1
        assert api().series_id(db, expected[0]['series_key']) == db.execute('SELECT series FROM observations LIMIT 1').fetchone()[0]
        assert api().series_id(db, 'absent') is None
    assert path.stat().st_size < old_path.stat().st_size * .4


@pytest.mark.parametrize('name,row', [
    ('node_metrics.csv',dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage='0',
                             unknown_numeric=' 1.0000000000000001 ',missing='',bad='bad',nonfinite='Infinity')),
    ('interface_metrics.csv',dict(timestamp='2026-09-17T04:00:00Z',node='br1',interface_id='ens4',
                                  rx_bytes_rate='000.0',new_metric='-0.0',invalid='nan')),
    ('routing_metrics.csv',dict(timestamp='2026-09-17T12:00:00',node='br1',metric_name='peer_total',
                                label='peer="西安",node_type="raw",label="collision"',value=' 1e309 ')),
    ('scrape_health.csv',dict(timestamp='2026-09-17T04:00:00Z',node='aux-service',target_id='srv',
                              exporter_type='custom',scrape_up='0',scrape_duration_seconds='bad')),
    ('traffic_flow_metrics.csv',dict(timestamp_utc='bad',prometheus_sample_time_utc='2026-09-17T04:00:00Z',
                                     source_region='xian',series_key='flow-a',flow_type='web',
                                     web_flow_bytes_total='100',dns_flow_qps='inactive')),
    ('netflow.csv',dict(minute_utc='2026-09-17T04:00:00Z',node_key='br1',interface_id='ens4',protocol='6',
                       bytes='0',packets='not numeric',flow_record_count='')),
    ('frr_syslog_events.csv',dict(event_time='bad',received_at='2026-09-17T04:00:00Z',hostname='br1',
                                 program='bgpd',severity='INFO',message='第一行\n第二行')),
])
def test_all_sources_keep_original_values_flags_dimensions_ids_and_byte_anchors(tmp_path,name,row):
    raw = tmp_path/'raw'
    write_csv(raw, name, [row])
    file = discover_sources(raw)[0]
    expected, _, profile = prepared(file)
    path = tmp_path/'spool.sqlite'
    manifest = parse_file((file,'stage2',path,settings(tmp_path)))
    with sqlite3.connect(path) as db:
        assert decoded(db) == expected
        assert manifest['essential_profile'] == profile
        assert list(iter_windows(decoded(db),'stage2')) == list(iter_windows(expected,'stage2'))
        anchor = decoded(db)[0]['ref']
    assert record_at(raw,'stage2',manifest,anchor).record_id == anchor['record_id']


def test_template_variants_and_numeric_types_are_lossless(tmp_path):
    raw = tmp_path/'raw'
    write_csv(raw,'node_metrics.csv',[dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage='1')])
    first = prepared(discover_sources(raw)[0])[0][0]
    changed = copy.deepcopy(first)
    changed['identity']['city'] = 'other city'
    changed['flags'] = ['custom_flag','invalid_numeric:custom']
    changed['raw_values'] = dict(custom=0.0, missing=None, malformed=' Infinity ', integer=2**65,
                                 negative_zero=-0.0, precise='1.000000000000000001')
    changed['ref']['source_file'] = 'other/source.csv'
    changed['ref']['custom_anchor'] = 'retained'
    with sqlite3.connect(':memory:') as db:
        codec = api().ObservationCodec(db, create=True)
        for row in (first,changed):
            series,payload = codec.encode(row)
            assert codec.decode(series,row['timestamp'],row['ref']['record_index'],payload) == row
        assert db.execute('SELECT count(*) FROM observation_templates').fetchone()[0] == 1


def test_checkpoint_resume_keeps_counts_and_completed_file_restores_missing_index(tmp_path):
    raw = tmp_path/'raw'
    path = write_csv(raw,'node_metrics.csv',[
        dict(timestamp=f'2026-09-17T04:0{i}:00Z',node='br1',cpu_usage=str(i)) for i in range(5)])
    file = discover_sources(raw)[0]
    expected, _, profile = prepared(file)
    spool = tmp_path/'spool.sqlite'
    def interrupt(event):
        if event['event'] == 'parse_checkpoint':
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        parse_file((file,'stage2',spool,settings(tmp_path)),interrupt)
    with sqlite3.connect(spool) as db:
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0] == 2
    events = []
    manifest = parse_file((file,'stage2',spool,settings(tmp_path)),events.append)
    assert manifest['essential_profile'] == profile
    assert any(e.get('event')=='parse_resumed' and e['record_index']==2 for e in events)
    with sqlite3.connect(spool) as db:
        assert decoded(db) == expected
        db.execute('DROP INDEX observations_order')
    reused = parse_file((file,'stage2',spool,settings(tmp_path)))
    assert reused == manifest
    with sqlite3.connect(spool) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='observations_order'").fetchone()


def test_legacy_checkpoint_and_payloads_resume_without_migration(tmp_path):
    raw = tmp_path/'raw'
    write_csv(raw,'interface_metrics.csv',[dict(timestamp=f'2026-09-17T04:0{i}:00Z',node='br1',
        interface_id='ens4',rx_bytes_rate=str(i)) for i in range(5)])
    file = discover_sources(raw)[0]
    expected, _, profile = prepared(file)
    spool = tmp_path/'legacy.sqlite'
    with PositionReader(file,'stage2') as reader:
        iterator = iter(reader)
        parser = PreparedParser(reader,'UTC',False)
        first = [parser.parse(next(iterator)) for _ in range(2)]
        snapshot = reader.checkpoint()
    saved = EssentialProfile()
    for row in first:saved.add(row)
    with sqlite3.connect(spool) as db:
        db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,payload BLOB); CREATE TABLE observations(series TEXT,time TEXT,idx INTEGER,payload BLOB)')
        for row in first:
            db.execute('INSERT INTO observations VALUES (?,?,?,?)',(row['series_key'],row['timestamp'],row['ref']['record_index'],db_dump(row)))
        db.execute("INSERT INTO metadata VALUES ('checkpoint',?)",(db_dump(dict(reader=snapshot,profile=saved.report())),))
    manifest = parse_file((file,'stage2',spool,settings(tmp_path)))
    assert manifest['essential_profile'] == profile
    with sqlite3.connect(spool) as db:
        assert decoded(db) == expected
        assert db.execute('SELECT DISTINCT typeof(series) FROM observations').fetchall() == [('text',)]
        assert api().series_keys(db) == [expected[0]['series_key']]
        assert api().series_id(db,expected[0]['series_key']) == expected[0]['series_key']


def test_short_extra_rejected_and_malformed_csv_preserve_checkpoint_behavior(tmp_path):
    raw = tmp_path/'raw';raw.mkdir()
    path = raw/'node_metrics.csv'
    path.write_text('timestamp,node,cpu_usage,new_metric\n'
                    '2026-09-17T04:00:00Z,br1,0\n'
                    'invalid,br1,3,9\n'
                    '2026-09-17T04:01:00Z,br1,bad,Infinity,extra\n')
    file = discover_sources(raw)[0]
    expected, _, profile = prepared(file)
    spool = tmp_path/'spool.sqlite'
    manifest = parse_file((file,'stage2',spool,settings(tmp_path)))
    assert manifest['essential_profile'] == profile
    assert profile['accepted']==2 and profile['rejected']==1
    with sqlite3.connect(spool) as db:assert decoded(db) == expected
    path.write_text(path.read_text()+'"unclosed,br1,4\n')
    broken = tmp_path/'broken.sqlite'
    with pytest.raises(ValueError,match='invalid CSV'):
        parse_file((file,'stage2',broken,settings(tmp_path)))
    with sqlite3.connect(broken) as db:
        assert not db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()
        assert load_json(db.execute("SELECT payload FROM metadata WHERE key='checkpoint'").fetchone()[0])['reader']['record_index'] == 2


def test_aggregate_series_mixed_private_sources_matches_legacy_merged_spool(tmp_path):
    from aiops_v4.features.spool import aggregate_series
    raw = tmp_path/'raw'
    common = dict(node='br1',metric_name='peer_total',label='peer="西安"')
    write_csv(raw,'routing_metrics_a.csv',[
        dict(common,timestamp='2026-09-17T04:01:00Z',value='160'),
        dict(common,timestamp='2026-09-17T04:00:00Z',value='100')])
    write_csv(raw,'routing_metrics_b.csv',[
        dict(common,timestamp='2026-09-17T04:00:00Z',value='100'),
        dict(common,timestamp='2026-09-17T04:02:00Z',value='200')])
    files = discover_sources(raw)
    sources = []
    merged = tmp_path/'merged.sqlite'
    with sqlite3.connect(merged) as old:
        old.execute('CREATE TABLE observations(series TEXT,time TEXT,file INTEGER,idx INTEGER,payload BLOB)')
        for file_index,file in enumerate(files):
            rows, _, _ = prepared(file)
            private = tmp_path/f'private_{file_index}.sqlite'
            if file_index == 0:
                parse_file((file,'stage2',private,settings(tmp_path)))
            else:
                # A completed old file and a newly compact file can coexist.
                with sqlite3.connect(private) as legacy:
                    legacy.execute('CREATE TABLE observations(series TEXT,time TEXT,idx INTEGER,payload BLOB)')
                    legacy.executemany('INSERT INTO observations VALUES (?,?,?,?)',[
                        (r['series_key'],r['timestamp'],r['ref']['record_index'],db_dump(r)) for r in rows])
            sources.append((file_index,str(private)))
            old.executemany('INSERT INTO observations VALUES (?,?,?,?,?)',[
                (r['series_key'],r['timestamp'],file_index,r['ref']['record_index'],db_dump(r)) for r in rows])
    key = rows[0]['series_key']
    outputs = []
    for index,source in enumerate((str(merged),sources)):
        result = aggregate_series((source,'stage2',key,settings(tmp_path),tmp_path/f'group_{index}.sqlite'))
        with sqlite3.connect(result['path']) as db:
            # Group workers now emit typed local dictionaries and statistics.
            from aiops_v4.features.store import create_store, StoreWriter, FeatureStore
            final_path=tmp_path/f'canonical_{index}.sqlite'
            final=create_store(final_path)
            writer=StoreWriter(final,'stage2',[dict(path=file.relative_path) for file in files])
            writer.import_prepared(db)
            final.execute("INSERT INTO metadata VALUES ('summary',?)",(db_dump(dict(batch='stage2',input_root=str(raw))),))
            final.commit();final.close()
            with FeatureStore(final_path,verify=False) as reader:
                outputs.append(list(reader.windows()))
    assert outputs[0] == outputs[1]
    assert outputs[1][1]['metrics']['peer_total']['delta_sum'] == 60
    assert outputs[1][0]['metrics']['peer_total']['duplicate_input_count'] == 1


def test_codec_rejects_truncated_and_inconsistent_typed_payloads(tmp_path):
    raw=tmp_path/'raw'
    write_csv(raw,'node_metrics.csv',[dict(timestamp='2026-09-17T04:00:00Z',node='br1',cpu_usage='1')])
    row=prepared(discover_sources(raw)[0])[0][0]
    module=api()
    with sqlite3.connect(':memory:') as db:
        codec=module.ObservationCodec(db,create=True)
        series,payload=codec.encode(row)
        arguments=(series,row['timestamp'],row['ref']['record_index'])
        with pytest.raises(ValueError,match='template'):
            codec.decode(series+1,*arguments[1:],payload)
        with pytest.raises(ValueError,match='truncated'):
            codec.decode(*arguments,module.MAGIC+b'\0'*32+b'\x80')
        offset=payload.index(b'V4Z1',len(module.MAGIC)+32)
        with pytest.raises(ValueError,match='metric values'):
            codec.decode(*arguments,payload[:offset]+db_dump([[],None,None,None]))
        with pytest.raises(ValueError,match='content ID'):
            codec.encode(dict(row,ref=dict(row['ref'],record_id='position:placeholder')))


@pytest.mark.skipif(not __import__('sys').platform.startswith('linux'),reason='Linux file descriptor audit')
@pytest.mark.parametrize('failure',['iterator','cancel','policy','target'])
def test_aggregate_exception_closes_private_sources_with_traceback_retained(tmp_path,monkeypatch,failure):
    import os
    from datetime import datetime,timedelta,timezone
    from aiops_v4.data import workers
    from aiops_v4.features import aggregate,spool
    raw=tmp_path/'raw'
    count=1001 if failure=='cancel' else 300 if failure=='policy' else 3
    start=datetime(2026,9,17,4,tzinfo=timezone.utc)
    write_csv(raw,'node_metrics.csv',[
        dict(timestamp=(start+timedelta(minutes=0 if failure=='cancel' else i)).isoformat(),node='br1',cpu_usage=str(i)) for i in range(count)])
    file=discover_sources(raw)[0]
    source=tmp_path/'private.sqlite'
    policy=settings(tmp_path)
    parse_file((file,'stage2',source,policy))
    db=sqlite3.connect(source)
    try:key=api().series_keys(db)[0]
    finally:db.close()
    def descriptors():
        found=[]
        for path in __import__('pathlib').Path('/proc/self/fd').iterdir():
            try:name=os.readlink(path)
            except FileNotFoundError:continue
            if name == str(source.resolve()):found.append(path.name)
        return found
    expected=OSError
    target=tmp_path/'group.sqlite'
    def forced_failure():
        assert descriptors(), 'test must observe the real open private SQLite source'
        raise expected('forced aggregate failure')
    if failure=='iterator':
        def fail_while_reading(ordered_records,*args):
            next(ordered_records)
            forced_failure()
            yield  # Keep the same generator API as iter_windows.
        monkeypatch.setattr(aggregate,'iter_windows',fail_while_reading)
    elif failure=='cancel':
        expected=workers.WorkerCancelled
        checks=[]
        def cancel_after_opening_sources():
            checks.append(None)
            if len(checks)==2:forced_failure()
        monkeypatch.setattr(workers,'check_cancel',cancel_after_opening_sources)
    elif failure=='policy':
        monkeypatch.setattr(spool.ResourcePolicy,'check',lambda self:forced_failure())
    else:
        expected=sqlite3.OperationalError
        original_connect=sqlite3.connect
        class FailingTarget(sqlite3.Connection):
            def execute(self,sql,*args):
                if sql.startswith('INSERT INTO windows'):forced_failure()
                return super().execute(sql,*args)
        def connect(path,*args,**kwargs):
            if path==target:kwargs['factory']=FailingTarget
            return original_connect(path,*args,**kwargs)
        monkeypatch.setattr(sqlite3,'connect',connect)
    assert not descriptors()
    with pytest.raises(expected,match='forced aggregate failure') as retained:
        spool.aggregate_series(([(0,str(source))],'stage2',key,policy,target))
    # Retaining traceback frames must not postpone source cleanup until GC.
    assert retained.value.__traceback__ is not None
    assert not descriptors()

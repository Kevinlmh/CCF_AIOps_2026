import gzip
import hashlib
import json
import pytest

from test_evidence_pipeline import inputs
from test_experiment_config import config


def test_compact_run_preserves_logical_hashes_queries_and_seal(tmp_path):
    from aiops_v4.experiments.run import run_experiment, verify_run
    from aiops_v4.evidence.index import query_evidence
    raw, _, _ = inputs(tmp_path, interfaces=0, spike=True)
    c = config(raw)
    c['agents'] = {'discovery_only': True}
    c['storage'] = {'compact': True}
    out = tmp_path / 'compact'
    s = run_experiment(c, out)
    windows = out / 'features' / 'windows.jsonl.gz'
    assert windows.exists() and not windows.with_suffix('').exists()
    logical = gzip.decompress(windows.read_bytes())
    assert hashlib.sha256(logical).hexdigest() == s['stages']['states']['input_windows_sha256']
    assert (out / 'states' / 'matrix.jsonl.gz').exists()
    rows = query_evidence(out / 'evidence' / 'evidence.sqlite', 'stage2', 'states', limit=1)
    assert rows and rows[0]['matrix']['batch'] == 'stage2'
    assert verify_run(out)['status'] == 'completed'
    windows.write_bytes(windows.read_bytes()[:-8])
    import pytest
    with pytest.raises(ValueError):
        verify_run(out)


def test_database_codec_reads_legacy_text_and_compressed_values():
    from aiops_v4.storage import db_dump, load_json
    value = {'large': ['repeated text'] * 100, 'count': 0}
    encoded = db_dump(value)
    assert isinstance(encoded, bytes) and len(encoded) < len(json.dumps(value)) / 4
    assert load_json(encoded) == load_json(json.dumps(value)) == value


def test_streaming_netflow_cross_file_unordered_counts_and_bounded_refs(tmp_path):
    from aiops_v4.features.pipeline import build_features
    from test_records import write_csv
    raw = tmp_path / 'raw'
    fields = dict(node_key='br1', interface_id='ens4', protocol='6')
    write_csv(raw, 'netflow_b.csv', [dict(fields, minute_utc='2026-09-17T04:00:00Z', bytes=str(i), packets='2') for i in range(100)])
    write_csv(raw, 'netflow_a.csv', [dict(fields, minute_utc='2026-09-17T04:01:00Z', bytes='7', packets='-1'),
                                     dict(fields, minute_utc='2026-09-17T04:00:00Z', bytes='5', packets='')])
    s = build_features(raw, 'stage2', tmp_path/'out', naive_timezone='UTC', netflow_streaming=True)
    rows = [json.loads(line) for line in (tmp_path/'out'/'windows.jsonl').read_text().splitlines()]
    assert s['netflow_streaming'] and s['raw_sort_rows'] == 0
    assert len(rows) == 2 and sum(r['record_count'] for r in rows) == 102
    first, second = rows
    assert first['metrics']['bytes']['sum'] == 4955
    assert first['metrics']['bytes']['mean'] == pytest.approx(4955 / 101)
    assert first['coverage']['observed_timestamps'] == 1
    assert len(first['references']) == 8 and first['references_truncated']
    assert second['metrics']['packets']['invalid_input_count'] == 1
    assert first['metrics']['bytes']['quantile_method'] == 'not_computed_streaming_netflow'


def test_compact_windows_are_compressed_before_publication(tmp_path, monkeypatch):
    from aiops_v4.features import pipeline
    from test_records import write_csv
    raw = tmp_path/'raw'
    write_csv(raw, 'node_metrics.csv', [dict(timestamp='2026-09-17T04:00:00Z', node='br1', cpu_usage='50')])
    original = pipeline._publish
    def inspect(artifacts, destination):
        assert (artifacts/'windows.jsonl.gz').exists()
        assert not (artifacts/'windows.jsonl').exists()
        original(artifacts, destination)
    monkeypatch.setattr(pipeline, '_publish', inspect)
    pipeline.build_features(raw, 'stage2', tmp_path/'out', naive_timezone='UTC', compact=True)


def test_streaming_netflow_finite_input_overflow_is_flagged(tmp_path):
    from aiops_v4.features.pipeline import build_features
    from test_records import write_csv
    raw=tmp_path/'raw'
    write_csv(raw, 'netflow.csv', [dict(minute_utc='2026-09-17T04:00:00Z', node_key='br1',
                                      interface_id='ens4', protocol='6', bytes='1e308')]*3)
    build_features(raw, 'stage2', tmp_path/'out', naive_timezone='UTC', netflow_streaming=True)
    row=json.loads((tmp_path/'out/windows.jsonl').read_text())
    metric=row['metrics']['bytes']
    assert metric['sum'] is None and 'sum' in metric['overflowed_statistics']


def test_netflow_eviction_reloads_window_and_preserves_timestamps_and_moments(tmp_path):
    import sqlite3
    from aiops_v4.features.netflow import NetflowAccumulator
    from aiops_v4.features.series import prepare_record
    from test_records import read
    records, _ = read(tmp_path, 'netflow.csv', [
        dict(minute_utc=f'2026-09-17T04:0{minute}:00Z', node_key='br1', interface_id='ens4', protocol='6', bytes=str(value))
        for minute, value in [(0, 10), (1, 1), (0, 30), (1, 2), (0, 20)]])
    with sqlite3.connect(':memory:') as db:
        reducer = NetflowAccumulator(db,60,None,capacity=1)
        for record in records:
            reducer.add(prepare_record(record,'UTC'))
        a,b = list(reducer.windows())
    assert a['metrics']['bytes']['sum'] == 60 and a['metrics']['bytes']['mean'] == 20
    assert a['metrics']['bytes']['std'] == pytest.approx((200/3)**.5)
    assert a['coverage']['observed_timestamps'] == 1 and a['record_count'] == 3
    assert b['metrics']['bytes']['sum'] == 3

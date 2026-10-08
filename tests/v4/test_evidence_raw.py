"""Raw retrieval must prove content, provenance and the upstream scan boundary."""
import pytest
from test_records import read
from test_evidence_bundles import module


def reference(record):
    return {key: getattr(record, key) for key in ('record_id', 'source', 'source_file', 'record_index', 'line_start', 'line_end')}


def manifest(record, reader):
    return [{'path': record.source_file, 'source': record.source, 'rows_scanned': reader.rows_scanned,
             'columns': reader.columns, 'complete': reader.complete}]


def test_raw_preserves_full_multiline_log_and_reinterprets_naive_time(tmp_path):
    message = 'BGP failure\n' + 'x' * 262144
    records, reader = read(tmp_path, 'frr_syslog_events.csv', [
        {'event_time': '2026-09-17 12:00:01', 'hostname': 'br1', 'message': message}])
    rows = list(module('raw').resolve_references(tmp_path, 'stage2', [reference(records[0])],
                                               manifest(records[0], reader), 'Asia/Shanghai'))
    assert len(rows) == 1
    assert rows[0]['raw']['message'] == rows[0]['text']['message'] == message
    assert (rows[0]['line_start'], rows[0]['line_end']) == (2, 3)
    assert rows[0]['observed_time'] == '2026-09-17T04:00:01.000000Z'
    assert rows[0]['timestamp'] == '2026-09-17T12:00:01.000000Z'
    assert rows[0]['time_quality_flags'] == ['naive_timezone:Asia/Shanghai']


def test_raw_deduplicates_and_stops_at_selected_prefix(tmp_path):
    records, reader = read(tmp_path, 'node_metrics.csv', [
        {'timestamp': '2026-09-17T04:00:00Z', 'node': 'br1', 'cpu_usage': str(i)} for i in range(3)])
    path = tmp_path / records[0].source_file
    # Invalid CSV tail is outside the requested prefix; reader must not consume it.
    with path.open('a') as f:
        f.write('"unfinished\n')
    refs = [reference(records[1]), reference(records[0]), reference(records[0])]
    rows = list(module('raw').resolve_references(tmp_path, 'stage2', iter(refs), manifest(records[0], reader), 'UTC'))
    assert [r['record_index'] for r in rows] == [1, 2]
    assert [r['raw']['cpu_usage'] for r in rows] == ['0', '1']


@pytest.mark.parametrize('change', [{'record_id': 'changed'}, {'line_end': 100}, {'source': 'routing'},
                                    {'source_file': '../outside.csv'}, {'record_index': 0}])
def test_raw_rejects_invalid_provenance(tmp_path, change):
    records, reader = read(tmp_path, 'node_metrics.csv', [{'timestamp': '2026-09-17T04:00:00Z', 'node': 'br1'}])
    ref = dict(reference(records[0]), **change)
    with pytest.raises(ValueError):
        list(module('raw').resolve_references(tmp_path, 'stage2', [ref], manifest(records[0], reader), 'UTC'))


def test_raw_rejects_batch_change_and_upstream_prefix_overrun(tmp_path):
    records, reader = read(tmp_path, 'node_metrics.csv', [
        {'timestamp': '2026-09-17T04:00:00Z', 'node': 'br1', 'cpu_usage': str(i)} for i in range(2)])
    for batch, refs, files in [('other', [reference(records[0])], manifest(records[0], reader)),
                              ('stage2', [reference(records[1])], [dict(manifest(records[0], reader)[0], rows_scanned=1)])]:
        with pytest.raises(ValueError):
            list(module('raw').resolve_references(tmp_path, batch, refs, files, 'UTC'))
    path = tmp_path / records[0].source_file
    path.write_text(path.read_text().replace(',0', ',99'))
    with pytest.raises(ValueError, match='record|content'):
        list(module('raw').resolve_references(tmp_path, 'stage2', [reference(records[0])], manifest(records[0], reader), 'UTC'))


def test_raw_rejects_symlink_escape_even_with_registered_manifest(tmp_path):
    root = tmp_path / 'raw'
    root.mkdir()
    records, reader = read(tmp_path / 'outside', 'node_metrics.csv', [{'timestamp': '2026-09-17T04:00:00Z', 'node': 'br1'}])
    path = root / records[0].source_file
    path.parent.mkdir(parents=True)
    path.symlink_to(tmp_path / 'outside' / records[0].source_file)
    with pytest.raises(ValueError, match='outside|escape'):
        list(module('raw').resolve_references(root, 'stage2', [reference(records[0])], manifest(records[0], reader), 'UTC'))

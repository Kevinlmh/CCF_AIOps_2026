"""Lossless source parsing and provenance, with no diagnosis transformations."""

import csv
from importlib import import_module
from importlib.util import find_spec

import pytest


def module(name):
    assert find_spec("aiops_v4") is not None, "v4 data foundation is not implemented"
    return import_module(f"aiops_v4.data.{name}")


def write_csv(root, filename, rows, columns=None):
    path = root / "xian" / "processed" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def read(root, filename, rows, batch="stage2", **kwargs):
    write_csv(root, filename, rows)
    file = module("discovery").discover_sources(root)[0]
    with module("reader").RecordReader(file, batch, **kwargs) as reader:
        records = list(reader)
    return records, reader


def test_discovers_all_sources_without_silently_dropping_raw_siblings(tmp_path):
    names = ["node_metrics.csv", "interface_metrics.csv", "routing_metrics.csv",
             "scrape_health.csv", "traffic_flow_metrics.csv", "netflow_5tuple_minute_readable.csv",
             "frr_syslog_events.csv"]
    for name in names:
        write_csv(tmp_path, name, [{"timestamp": "2026-09-17T04:00:00Z"}])
    raw = tmp_path / "xian" / "xian_data" / "node_metrics.csv"
    raw.parent.mkdir()
    raw.write_text("timestamp,node\n")
    (tmp_path / "answers.csv").write_text("prediction_id\n")
    files = module("discovery").discover_sources(tmp_path)
    assert sorted(file.source for file in files) == [
        "frr", "interface", "netflow", "node", "node", "routing", "scrape", "traffic",
    ]
    assert len({file.relative_path for file in files}) == 8


def test_preserves_zero_missing_bad_numbers_and_raw_fields(tmp_path):
    records, _ = read(tmp_path, "node_metrics.csv", [{
        "timestamp": "2026-09-17T12:00:00+08:00", "node": "br1",
        "cpu_usage": "0", "load1": "", "load5": "broken", "disk_io_util": "Infinity",
    }])
    row = records[0]
    assert row.timestamp == "2026-09-17T04:00:00.000000Z"
    assert row.node_id == "xian-br-1"
    assert row.values == {"cpu_usage": 0.0}
    assert row.raw["load1"] == "" and row.raw["load5"] == "broken"
    assert "invalid_numeric:load5" in row.quality_flags
    assert "nonfinite_numeric:disk_io_util" in row.quality_flags


@pytest.mark.parametrize("node,expected", [("br-1", "xian-br-1"), ("br1", "xian-br-1"),
    ("BR-1-ccf-aiops-西安", "xian-br-1"), ("br-10", None), ("service-vm-10", None)])
def test_role_mapping_respects_name_boundaries(tmp_path, node, expected):
    rows, _ = read(tmp_path, "node_metrics.csv", [{
        "timestamp": "2026-09-17T04:00:00Z", "node": node, "cpu_usage": "10",
    }])
    assert rows[0].node_id == expected
    if expected is None:
        assert "unmapped_entity" in rows[0].quality_flags


def test_preserves_full_multiline_log_and_physical_line_provenance(tmp_path):
    message = "BGP failure\n" + "原始详细信息" * 100
    records, reader = read(tmp_path, "frr_syslog_events.csv", [{
        "event_time": "2026-09-17 04:00:01", "received_at": "2026-09-17 04:00:02",
        "hostname": "br-1", "program": "bgpd", "severity": "error", "message": message,
    }])
    row = records[0]
    assert row.text["message"] == message and row.raw["message"] == message
    assert (row.line_start, row.line_end, row.record_index) == (2, 3, 1)
    assert "assumed_utc" in row.quality_flags
    assert reader.complete and reader.rows_scanned == 1


def test_routing_peer_labels_are_retained_without_aggregation(tmp_path):
    records, _ = read(tmp_path, "routing_metrics.csv", [{
        "timestamp": "2026-09-17T04:00:00Z", "node": "br-1",
        "metric_name": "bgp_peer_up", "label": 'peer="2001:db8::2",afi="ipv6"', "value": "0",
    }])
    assert records[0].values == {"bgp_peer_up": 0.0}
    assert records[0].dimensions["peer"] == "2001:db8::2"
    assert records[0].dimensions["label"] == 'peer="2001:db8::2",afi="ipv6"'


def test_traffic_counters_are_kept_before_differencing_and_no_target_vm_is_inferred(tmp_path):
    records, _ = read(tmp_path, "traffic_flow_metrics.csv", [{
        "timestamp_utc": "2026-09-17T04:00:00Z", "flow_type": "dns", "series_key": "a",
        "source_region": "xian", "target_region": "beida", "dns_flow_requests_total": "100",
    }])
    assert records[0].values == {"dns_flow_requests_total": 100.0}
    assert records[0].node_id == "xian-traffic-vm"
    assert records[0].dimensions["target_region"] == "beida"
    assert "entity_from_source_role" in records[0].quality_flags


def test_netflow_endpoints_ports_and_raw_counts_survive(tmp_path):
    records, _ = read(tmp_path, "netflow_5tuple_minute_readable.csv", [{
        "minute_utc": "2026-09-17T04:00:00Z", "node_key": "br1", "interface_id": "ens4",
        "protocol": "6", "src_addr": "2001:db8::1", "src_port": "179",
        "dst_addr": "2001:db8::2", "dst_port": "40000", "packets": "4", "bytes": "326",
        "flow_record_count": "2",
    }])
    assert records[0].values == {"packets": 4.0, "bytes": 326.0, "flow_record_count": 2.0}
    assert records[0].dimensions["src_port"] == "179"
    assert records[0].dimensions["dst_addr"] == "2001:db8::2"


@pytest.mark.parametrize("filename,row,metric,value", [
    ("interface_metrics.csv", {"interface_id": "ens10", "rx_bytes_rate": "30"}, "rx_bytes_rate", 30),
    ("scrape_health.csv", {"target_id": "a:9100", "exporter_type": "node", "scrape_up": "0",
                          "scrape_error": "request timeout"}, "scrape_up", 0),
])
def test_interface_and_scrape_metrics_keep_identity(tmp_path, filename, row, metric, value):
    records, _ = read(tmp_path, filename, [{"timestamp": "2026-09-17T04:00:00Z", "node": "br1", **row}])
    assert records[0].values[metric] == value
    if filename.startswith("scrape"):
        assert records[0].text["scrape_error"] == "request timeout"


def test_bad_time_and_unmapped_entity_rows_are_not_discarded(tmp_path):
    records, _ = read(tmp_path, "node_metrics.csv", [{
        "timestamp": "broken", "node": "mystery", "cpu_usage": "10",
    }])
    assert len(records) == 1 and records[0].timestamp is None
    assert {"invalid_timestamp", "unmapped_entity"} <= set(records[0].quality_flags)


def test_record_ids_are_repeatable_and_isolated_by_batch(tmp_path):
    write_csv(tmp_path, "node_metrics.csv", [{"timestamp": "2026-09-17T04:00:00Z", "node": "br1"}])
    file = module("discovery").discover_sources(tmp_path)[0]
    result = []
    for batch in ["stage1", "stage1", "stage2"]:
        with module("reader").RecordReader(file, batch) as reader:
            result.append(next(iter(reader)).record_id)
    assert result[0] == result[1] and result[0] != result[2]


def test_prefix_sampling_marks_incomplete_file(tmp_path):
    records, reader = read(tmp_path, "node_metrics.csv", [
        {"timestamp": f"2026-09-17T04:0{i}:00Z", "node": "br1"} for i in range(2)
    ], max_rows=1)
    assert len(records) == 1 and reader.rows_scanned == 1 and not reader.complete


def test_empty_file_is_valid_but_duplicate_columns_are_rejected(tmp_path):
    path = tmp_path / "node_metrics.csv"
    path.write_text("")
    file = module("discovery").discover_sources(tmp_path)[0]
    with module("reader").RecordReader(file, "sample") as reader:
        assert list(reader) == []
    assert reader.complete
    path.write_text("timestamp,node,node\n2026-09-17T04:00:00Z,br1,br2\n")
    with pytest.raises(ValueError, match="duplicate"):
        with module("reader").RecordReader(file, "sample") as reader:
            list(reader)
def test_large_csv_log_field_is_preserved_without_default_csv_size_cutoff(tmp_path):
    message = "full\n" + "x" * (256 * 1024)
    write_csv(tmp_path, "frr_syslog_events.csv", [{
        "event_time": "2026-09-17T04:00:00Z", "hostname": "br1", "message": message,
    }])
    reader_module = module("reader")
    file = module("discovery").discover_sources(tmp_path)[0]
    with reader_module.RecordReader(file, "stage1") as reader:
        rows = list(reader)
    assert rows[0].raw["message"] == rows[0].text["message"] == message
    assert (rows[0].line_start, rows[0].line_end) == (2, 3)

"""The v3 raw entrypoint uses the full v2 canonical feature projection."""

from __future__ import annotations

import csv
from pathlib import Path
import shutil

import pytest

from aiops_v3.raw import build_sample_store
from aiops_v3.store import open_store


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _bundle(root: Path) -> Path:
    folder = root / "case_001" / "20260728040000_20260728040400" / "xian_20260728040000_20260728040400" / "processed"
    t0, t1, t2, t3 = (f"2026-07-28 04:0{i}:00" for i in range(4))
    _write(folder / "node_metrics.csv", [
        {"timestamp": t0, "node": "br-1", "cpu_usage": "20", "cpu_user": "10"},
        {"timestamp": t3, "node": "br-1", "cpu_usage": "30", "cpu_user": "15"},
    ])
    _write(folder / "interface_metrics.csv", [
        {"timestamp": t1, "node": "br-1", "interface_id": "ens8", "rx_error_rate": "2"},
    ])
    _write(folder / "routing_metrics.csv", [
        {"timestamp": t1, "node": "br-1", "metric_name": "ospf6_interface_cost", "label": 'interface="ens8"', "value": "100"},
    ])
    _write(folder / "scrape_health.csv", [
        {"timestamp": t1, "node": "br-1", "target_id": "x", "exporter_type": "node", "scrape_up": "1"},
    ])
    _write(folder / "netflow_5tuple_minute_readable.csv", [
        {"minute_utc": t1, "node_key": "br1", "interface_id": "ens8", "protocol": "6", "packets": "2", "bytes": "100"},
    ])
    _write(folder / "frr_syslog_events.csv", [
        {"event_time": t1, "hostname": "br-1", "severity": "error", "program": "bgpd", "message": "BGP peer down"},
    ])
    traffic = []
    for timestamp, small_requests, small_errors, large_requests in (
        (t1, 10, 0, 90), (t2, 20, 5, 180),
    ):
        for identity, requests, errors in (
            ("small", small_requests, small_errors),
            ("large", large_requests, 0),
        ):
            traffic.append({
                "timestamp_utc": timestamp, "series_key": identity,
                "flow_type": "dns", "source_region": "xian", "target_region": "beida",
                "dns_flow_requests_total": str(requests),
                "dns_flow_error_total": str(errors),
            })
    _write(folder / "traffic_flow_metrics.csv", traffic)
    return folder


def test_canonical_build_preserves_broad_features_and_weighted_error_ratio(tmp_path: Path) -> None:
    _bundle(tmp_path)
    path = build_sample_store(tmp_path / "case_001", tmp_path / "store")
    store = open_store(path)
    assert store.manifest["format_version"] == 4
    assert store.feature_index("node", "node.cpu_user") is not None
    assert store.observed_node(0, store.nodes.index("xian-br-1"), "node.cpu_user") == 10
    assert store.observed_node(1, store.nodes.index("xian-br-1"), "routing.ospf6_interface_cost") == 100
    edge = store.edges.index({"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"})
    ratio = store.feature_index("edge", "traffic.dns.error_ratio")
    assert ratio is not None
    assert store.edge_values[2, edge, ratio] == pytest.approx(.05)
    assert {"source": "xian-br-1", "target": "interface:xian-br-1:ens8", "relation": "netflow"} in store.edges
    assert (path / "dimension_cells.npy").exists()
    assert store.manifest["parser_audit"]["quality_status"] == "clean"
    assert store.manifest["parser_audit"]["files_by_source"] == {
        "node": 1, "interface": 1, "routing": 1, "scrape": 1,
        "traffic": 1, "netflow": 1, "frr": 1,
    }


def test_canonical_build_rejects_incomplete_source_bundle(tmp_path: Path) -> None:
    folder = tmp_path / "case_001" / "20260728040000_20260728040400" / "xian_20260728040000_20260728040400" / "processed"
    _write(folder / "node_metrics.csv", [{"timestamp": "2026-07-28 04:01:00", "node": "br-1", "cpu_usage": "30"}])
    with pytest.raises(ValueError, match="missing required source"):
        build_sample_store(tmp_path / "case_001", tmp_path / "store")
    assert not (tmp_path / "store").exists()


def test_service_flows_keep_distinct_edges(tmp_path: Path) -> None:
    folder = _bundle(tmp_path)
    _write(folder / "traffic_flow_metrics.csv", [
        {"timestamp_utc": f"2026-07-28 04:0{minute}:00", "series_key": flow,
         "flow_type": flow, "source_region": "xian", "target_region": "beida",
         "dns_flow_requests_total": str(10 * minute) if flow == "dns" else "",
         "dns_flow_error_total": str(5 * (minute - 1)) if flow == "dns" else "",
         "web_flow_requests_total": str(10 * minute) if flow == "web" else "",
         "web_flow_error_total": "0" if flow == "web" else ""}
        for minute in (1, 2) for flow in ("dns", "web")
    ])
    store = open_store(build_sample_store(tmp_path / "case_001", tmp_path / "store"))
    dns = store.edges.index({"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"})
    web = store.edges.index({"source": "xian-traffic-vm", "target": "service-group:beida:web", "relation": "traffic"})
    assert dns != web
    assert store.edge_values[2, dns, store.feature_index("edge", "traffic.dns.error_ratio")] == pytest.approx(.5)
    assert store.edge_values[2, web, store.feature_index("edge", "traffic.web.error_ratio")] == 0


def test_same_series_key_in_different_flows_has_independent_counters(tmp_path: Path) -> None:
    folder = _bundle(tmp_path)
    _write(folder / "traffic_flow_metrics.csv", [
        {"timestamp_utc": f"2026-07-28 04:0{minute}:00", "series_key": "shared",
         "flow_type": flow, "source_region": "xian", "target_region": "beida",
         "dns_flow_requests_total": str(10 * minute) if flow == "dns" else "",
         "dns_flow_error_total": "0" if flow == "dns" else "",
         "web_flow_requests_total": str(20 * minute) if flow == "web" else "",
         "web_flow_error_total": "0" if flow == "web" else ""}
        for minute in (1, 2) for flow in ("dns", "web")
    ])
    store = open_store(build_sample_store(tmp_path / "case_001", tmp_path / "store"))
    for flow, expected in (("dns", 10.0), ("web", 20.0)):
        edge = store.edges.index({"source": "xian-traffic-vm", "target": f"service-group:beida:{flow}", "relation": "traffic"})
        rate = store.feature_index("edge", f"traffic.{flow}.requests_rate")
        assert store.edge_values[2, edge, rate] == pytest.approx(expected)
    assert store.manifest["parser_audit"]["out_of_order_rows_by_source"] == {}


def test_request_rate_accounts_for_missing_minutes(tmp_path: Path) -> None:
    folder = _bundle(tmp_path)
    _write(folder / "traffic_flow_metrics.csv", [
        {"timestamp_utc": f"2026-07-28 04:0{minute}:00", "series_key": "dns",
         "flow_type": "dns", "source_region": "xian", "target_region": "beida",
         "dns_flow_requests_total": str(requests), "dns_flow_error_total": "0"}
        for minute, requests in ((1, 10), (3, 130))
    ])
    store = open_store(build_sample_store(tmp_path / "case_001", tmp_path / "store"))
    edge = store.edges.index({"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"})
    rate = store.feature_index("edge", "traffic.dns.requests_rate")
    assert store.edge_values[3, edge, rate] == pytest.approx(60.0)  # requests per minute


def test_low_routing_state_keeps_any_down_peer(tmp_path: Path) -> None:
    folder = _bundle(tmp_path)
    _write(folder / "routing_metrics.csv", [
        {"timestamp": "2026-07-28 04:01:00", "node": "br-1", "metric_name": "bgp_peer_up",
         "label": f'peer="{peer}"', "value": str(value)}
        for peer, value in (("a", 1), ("b", 0))
    ])
    store = open_store(build_sample_store(tmp_path / "case_001", tmp_path / "store"))
    assert store.observed_node(1, store.nodes.index("xian-br-1"), "routing.bgp_peer_up") == 0


def test_independent_cases_cannot_be_combined(tmp_path: Path) -> None:
    folder = _bundle(tmp_path)
    shutil.copytree(folder.parent.parent, tmp_path / "case_002" / folder.parent.parent.name)
    with pytest.raises(ValueError, match="independent cases"):
        build_sample_store(tmp_path, tmp_path / "store")


def test_parser_audit_records_unmapped_rows(tmp_path: Path) -> None:
    folder = _bundle(tmp_path)
    _write(folder / "node_metrics.csv", [
        {"timestamp": "2026-07-28 04:00:00", "node": "br-1", "cpu_usage": "20"},
        {"timestamp": "2026-07-28 04:01:00", "node": "probe-vm", "cpu_usage": "20"},
        {"timestamp": "2026-07-28 04:03:00", "node": "br-1", "cpu_usage": "30"},
    ])
    manifest = open_store(build_sample_store(tmp_path / "case_001", tmp_path / "store")).manifest
    assert manifest["parser_audit"]["rows_by_source"]["node"] == 3
    assert manifest["parser_audit"]["unmapped_entities_by_source"]["node"] == 1


def test_invalid_numeric_data_blocks_publication(tmp_path: Path) -> None:
    folder = _bundle(tmp_path)
    _write(folder / "node_metrics.csv", [
        {"timestamp": "2026-07-28 04:00:00", "node": "br-1", "cpu_usage": "invalid"},
        {"timestamp": "2026-07-28 04:03:00", "node": "br-1", "cpu_usage": "30"},
    ])
    with pytest.raises(ValueError, match="parser quality"):
        build_sample_store(tmp_path / "case_001", tmp_path / "store")
    assert not (tmp_path / "store").exists()

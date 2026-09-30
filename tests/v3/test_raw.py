import csv

import pytest

from aiops_v3.raw import build_sample_store
from aiops_v3.store import open_store


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def test_seven_sources_build_masked_store_and_counter_reset(tmp_path):
    root = tmp_path / "case_001" / "20260728040000_20260728040400" / "xian_20260728040000_20260728040400" / "processed"
    t1, t2 = "2026-07-28 04:01:00", "2026-07-28 04:02:00"
    write_csv(root / "node_metrics.csv", [{"timestamp": t1, "node": "br-1", "cpu_usage": "30"}])
    write_csv(root / "interface_metrics.csv", [{"timestamp": t1, "node": "br-1", "rx_error_rate": "1"}])
    write_csv(root / "routing_metrics.csv", [
        {"timestamp": t1, "node": "br-1", "metric_name": "bgp_peer_up", "value": "0"},
        {"timestamp": t1, "node": "br-1", "metric_name": "ospf6_interface_cost", "value": "100"},
    ])
    write_csv(root / "scrape_health.csv", [{"timestamp": t1, "node": "br-1", "scrape_up": "1"}])
    write_csv(root / "netflow_5tuple_minute_readable.csv", [{"minute_utc": t1, "node_key": "br1", "interface_id": "ens4", "protocol": "6", "bytes": "100"}])
    write_csv(root / "frr_syslog_events.csv", [{"event_time": t1, "hostname": "br-1", "severity": "error", "message": "BGP peer down"}])
    write_csv(root / "traffic_flow_metrics.csv", [
        {"timestamp_utc": t1, "series_key": "a", "flow_type": "dns", "source_region": "xian", "target_region": "beida", "dns_flow_requests_total": "10", "dns_flow_error_total": "2"},
        {"timestamp_utc": t2, "series_key": "a", "flow_type": "dns", "source_region": "xian", "target_region": "beida", "dns_flow_requests_total": "5", "dns_flow_error_total": "1"},
    ])

    store = open_store(build_sample_store(tmp_path, tmp_path / "out"))
    node = store.nodes.index("xian-br-1")
    assert store.observed_node(1, node, "node.cpu_usage") == 30
    assert store.observed_node(1, node, "routing.ospf6_interface_cost") == 100
    assert store.observed_node(1, node, "netflow.bytes") == 100
    netflow_edge = store.edges.index({"source": "xian-br-1", "target": "interface:xian-br-1:ens4", "relation": "netflow"})
    assert store.edge_values[1, netflow_edge, store.feature_index("edge", "netflow.bytes.protocol_6")] == 100
    assert store.observed_node(0, node, "node.cpu_usage") is None
    edge = store.edges.index({"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"})
    feature = store.feature_index("edge", "traffic.dns.error_ratio")
    assert store.edge_mask[2, edge, feature]
    assert abs(store.edge_values[2, edge, feature] - 0.2) < 1e-6
    requests = store.feature_index("edge", "traffic.dns.requests_rate")
    assert requests is not None
    assert abs(float(store.edge_values[2, edge, requests]) - 5 / 60) < 1e-6
    assert set(store.manifest["source_counts"]) == {"node", "interface", "routing", "scrape", "netflow", "frr", "traffic"}


def test_distinct_services_in_one_city_get_distinct_edges(tmp_path):
    root = tmp_path / "case_001" / "20260728040000_20260728040400" / "xian_20260728040000_20260728040400" / "processed"
    rows = []
    for minute, dns_errors, web_errors in ((1, 0, 0), (2, 5, 0)):
        for flow, errors in (("dns", dns_errors), ("web", web_errors)):
            rows.append({
                "timestamp_utc": f"2026-07-28 04:0{minute}:00", "series_key": flow,
                "flow_type": flow, "source_region": "xian", "target_region": "beida",
                "dns_flow_requests_total": str(10 * minute) if flow == "dns" else "",
                "dns_flow_error_total": str(errors) if flow == "dns" else "",
                "web_flow_requests_total": str(10 * minute) if flow == "web" else "",
                "web_flow_error_total": str(errors) if flow == "web" else "",
            })
    write_csv(root / "traffic_flow_metrics.csv", rows)
    store = open_store(build_sample_store(tmp_path, tmp_path / "out"))
    dns = store.edges.index({"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"})
    web = store.edges.index({"source": "xian-traffic-vm", "target": "service-group:beida:web", "relation": "traffic"})
    assert dns != web
    assert store.edge_values[2, dns, store.feature_index("edge", "traffic.dns.error_ratio")] == .5
    assert store.edge_values[2, web, store.feature_index("edge", "traffic.web.error_ratio")] == 0


def test_service_error_ratio_weights_each_observed_series_by_requests(tmp_path):
    root = tmp_path / "case_001" / "20260728040000_20260728040400" / "xian_20260728040000_20260728040400" / "processed"
    rows = []
    for minute, small_errors, large_errors in ((1, 0, 0), (2, 5, 0)):
        for identity, requests, errors in (("small", 10 * minute, small_errors),
                                           ("large", 90 * minute, large_errors)):
            rows.append({
                "timestamp_utc": f"2026-07-28 04:0{minute}:00", "series_key": identity,
                "flow_type": "dns", "source_region": "xian", "target_region": "beida",
                "dns_flow_requests_total": str(requests), "dns_flow_error_total": str(errors),
            })
    write_csv(root / "traffic_flow_metrics.csv", rows)
    store = open_store(build_sample_store(tmp_path, tmp_path / "out"))
    edge = store.edges.index({"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"})
    column = store.feature_index("edge", "traffic.dns.error_ratio")
    assert store.edge_mask[2, edge, column]
    assert abs(float(store.edge_values[2, edge, column]) - .05) < 1e-6


def test_request_rate_uses_elapsed_time_across_missing_minutes(tmp_path):
    root = tmp_path / "case_001" / "20260728040000_20260728040500" / "xian_20260728040000_20260728040500" / "processed"
    write_csv(root / "traffic_flow_metrics.csv", [
        {"timestamp_utc": "2026-07-28 04:01:00", "series_key": "dns", "flow_type": "dns",
         "source_region": "xian", "target_region": "beida", "dns_flow_requests_total": "10", "dns_flow_error_total": "0"},
        {"timestamp_utc": "2026-07-28 04:03:00", "series_key": "dns", "flow_type": "dns",
         "source_region": "xian", "target_region": "beida", "dns_flow_requests_total": "130", "dns_flow_error_total": "0"},
    ])
    store = open_store(build_sample_store(tmp_path, tmp_path / "out"))
    edge = store.edges.index({"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"})
    requests = store.feature_index("edge", "traffic.dns.requests_rate")
    assert abs(float(store.edge_values[3, edge, requests]) - 1.0) < 1e-6


def test_independent_sample_cases_cannot_be_silently_combined(tmp_path):
    for case in ("case_001", "case_002"):
        root = tmp_path / case / "20260728040000_20260728040400" / "xian_20260728040000_20260728040400" / "processed"
        write_csv(root / "node_metrics.csv", [
            {"timestamp": "2026-07-28 04:01:00", "node": "br-1", "cpu_usage": "10"},
        ])
    with pytest.raises(ValueError, match="independent cases"):
        build_sample_store(tmp_path, tmp_path / "out")


def test_stage_one_region_data_layout_is_discovered(tmp_path):
    root = tmp_path / "regions" / "xian_20260819040000_20260819040400" / "xian_20260819040000_20260819040400_data"
    write_csv(root / "node_metrics_20260819040000_20260819040400.csv", [
        {"timestamp": "2026-08-19 04:01:00", "node": "br-1", "cpu_usage": "51"},
    ])
    store = open_store(build_sample_store(tmp_path, tmp_path / "out"))
    assert store.observed_node(1, store.nodes.index("xian-br-1"), "node.cpu_usage") == 51


def test_any_down_bgp_peer_is_retained_within_one_minute(tmp_path):
    root = tmp_path / "case_001" / "20260728040000_20260728040400" / "xian_20260728040000_20260728040400" / "processed"
    write_csv(root / "routing_metrics.csv", [
        {"timestamp": "2026-07-28 04:01:00", "node": "br-1", "metric_name": "bgp_peer_up", "value": "1"},
        {"timestamp": "2026-07-28 04:01:00", "node": "br-1", "metric_name": "bgp_peer_up", "value": "0"},
    ])
    store = open_store(build_sample_store(tmp_path, tmp_path / "out"))
    assert store.observed_node(1, store.nodes.index("xian-br-1"), "routing.bgp_peer_up") == 0


def test_raw_manifest_accounts_for_every_row_including_unmapped_probe(tmp_path):
    root = tmp_path / "case_001" / "20260728040000_20260728040400" / "xian_20260728040000_20260728040400" / "processed"
    write_csv(root / "node_metrics.csv", [
        {"timestamp": "2026-07-28 04:01:00", "node": "br-1", "cpu_usage": "10"},
        {"timestamp": "2026-07-28 04:02:00", "node": "probe-vm", "cpu_usage": "10"},
    ])
    manifest = open_store(build_sample_store(tmp_path, tmp_path / "out")).manifest
    assert manifest["source_counts"]["node"] == 2
    assert manifest["accepted_rows_by_source"]["node"] == 1
    assert manifest["rejected_rows_by_source"]["node"] == 1

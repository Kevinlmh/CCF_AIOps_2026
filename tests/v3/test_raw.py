import csv

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
    write_csv(root / "routing_metrics.csv", [{"timestamp": t1, "node": "br-1", "metric_name": "bgp_peer_up", "value": "0"}])
    write_csv(root / "scrape_health.csv", [{"timestamp": t1, "node": "br-1", "scrape_up": "1"}])
    write_csv(root / "netflow_5tuple_minute_readable.csv", [{"minute_utc": t1, "node_key": "br1", "bytes": "100"}])
    write_csv(root / "frr_syslog_events.csv", [{"event_time": t1, "hostname": "br-1", "severity": "error", "message": "BGP peer down"}])
    write_csv(root / "traffic_flow_metrics.csv", [
        {"timestamp_utc": t1, "series_key": "a", "flow_type": "dns", "source_region": "xian", "target_region": "beida", "dns_flow_requests_total": "10", "dns_flow_error_total": "2"},
        {"timestamp_utc": t2, "series_key": "a", "flow_type": "dns", "source_region": "xian", "target_region": "beida", "dns_flow_requests_total": "5", "dns_flow_error_total": "1"},
    ])

    store = open_store(build_sample_store(tmp_path, tmp_path / "out"))
    node = store.nodes.index("xian-br-1")
    assert store.observed_node(1, node, "node.cpu_usage") == 30
    assert store.observed_node(1, node, "netflow.bytes") == 100
    assert store.observed_node(0, node, "node.cpu_usage") is None
    edge = store.edges.index({"source": "xian-traffic-vm", "target": "city:beida", "relation": "traffic"})
    feature = store.feature_index("edge", "traffic.dns.error_ratio")
    assert store.edge_mask[2, edge, feature]
    assert abs(store.edge_values[2, edge, feature] - 0.2) < 1e-6
    assert set(store.manifest["source_counts"]) == {"node", "interface", "routing", "scrape", "netflow", "frr", "traffic"}

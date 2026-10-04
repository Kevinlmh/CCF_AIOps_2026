from datetime import datetime, timezone
import json

import numpy as np

from aiops_v3.contracts import load_contract
from aiops_v3.detection import Event, Signal
from aiops_v3.detection import detect
from aiops_v3.diagnosis import Candidate, EvidencePack, build_evidence, diagnose_rules
from aiops_v3.store import open_store
from aiops_v3.data.feature_store import DimensionSeries


class TinyStore:
    def time_at(self, minute):
        return datetime(2026, 7, 28, 12, minute, tzinfo=timezone.utc)


def signal(node, category, score, feature, suffix):
    return Signal(f"ev-{suffix}", 5, node, feature, category, 80.0, 10.0, score, "direct")


def test_specific_memory_cause_beats_cpu_cosymptom_for_same_root():
    event = Event("v3-e000001", 5, 8, (
        signal("guangzhou-service-vm-3", "cpu_pressure", 7.3, "node.cpu_usage", "cpu"),
        signal("guangzhou-service-vm-3", "memory_pressure", 5.0, "node.memory_available_ratio", "mem"),
    ))
    pack = build_evidence(TinyStore(), event, load_contract())
    result = diagnose_rules(pack, load_contract())
    assert result.roots[0] == "guangzhou-service-vm-3"
    assert result.category == ("resource", "memory_pressure")
    assert len(result.roots) == len(set(result.roots)) == 5
    assert {"ev-cpu", "ev-mem"} <= set(result.evidence_ids)


def test_stronger_memory_anchor_beats_weaker_disk_anchor():
    event = Event("v3-e000005", 5, 8, (
        signal("guangzhou-service-vm-3", "memory_pressure", 10, "node.memory_available_ratio", "mem"),
        signal("guangzhou-service-vm-3", "disk_io_pressure", 5.5, "node.disk_io_util", "disk"),
    ))
    pack = build_evidence(TinyStore(), event, load_contract())
    assert diagnose_rules(pack, load_contract()).category == ("resource", "memory_pressure")


def test_weaker_duplicate_cannot_erase_strong_disk_anchor():
    event = Event("v3-e000006", 5, 8, (
        signal("xian-service-vm-1", "disk_io_pressure", 10, "node.disk_io_util", "strong"),
        signal("xian-service-vm-1", "cpu_pressure", 6, "node.cpu_usage", "cpu"),
        signal("xian-service-vm-1", "disk_io_pressure", 1, "node.disk_io_util", "weak"),
    ))
    pack = build_evidence(TinyStore(), event, load_contract())
    assert diagnose_rules(pack, load_contract()).category == ("resource", "disk_io_pressure")


def test_disk_anchor_prefers_disk_category_and_nonlegal_symptom_is_not_root():
    event = Event("v3-e000002", 5, 8, (
        signal("wuhan-service-vm-2", "cpu_pressure", 20, "node.cpu_usage", "cpu"),
        signal("wuhan-service-vm-2", "disk_io_pressure", 9, "node.disk_io_util", "disk"),
        Signal("ev-symptom", 6, "city:wuhan", "traffic.auth.latency_p95_seconds", "auth", .4, .1, 5, "symptom"),
    ))
    pack = build_evidence(TinyStore(), event, load_contract())
    result = diagnose_rules(pack, load_contract())
    assert result.roots[0] == "wuhan-service-vm-2"
    assert result.category == ("resource", "disk_io_pressure")
    assert "city:wuhan" not in result.roots
    assert any(candidate.node == "wuhan-service-vm-2" for candidate in pack.candidates)


def test_unmapped_remote_symptom_does_not_outrank_local_context():
    event = Event("v3-e000003", 5, 8, (
        signal("xian-service-vm-1", "cpu_pressure", 10, "node.cpu_usage", "cpu"),
        Signal("ev-remote", 6, "city:wuhan", "traffic.auth.latency_p95_seconds", "auth", .4, .1, 5, "symptom"),
    ))
    pack = build_evidence(TinyStore(), event, load_contract())
    assert pack.candidates[1].node.startswith("xian-")


def test_service_group_target_city_is_used_for_candidate_retrieval():
    event = Event("v3-e000004", 5, 8, (
        Signal("ev-service", 6, "service-group:wuhan:auth", "traffic.auth.error_ratio", "auth", .7, .01, 8, "symptom"),
    ))
    pack = build_evidence(TinyStore(), event, load_contract())
    assert pack.candidates[0].node.startswith("wuhan-")


def test_single_probe_service_symptom_keeps_observer_in_root_candidates():
    class TrafficStore(TinyStore):
        edges = [{"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"}]

    event = Event("v3-e000007", 5, 8, (
        Signal("edge:5:0:traffic.dns.error_ratio", 5, "service-group:beida:dns",
               "traffic.dns.error_ratio", "dns", 1, 0, 8, "symptom"),
    ))
    ordinary = build_evidence(TrafficStore(), event, load_contract())
    assert "xian-traffic-vm" not in [item.node for item in ordinary.candidates[:5]]
    pack = build_evidence(TrafficStore(), event, load_contract(), include_probe_candidates=True)
    assert pack.candidates[0].node.startswith("beida-")
    assert "xian-traffic-vm" in [item.node for item in pack.candidates[:5]]
    assert len({item.node for item in pack.candidates}) == len(pack.candidates)


def test_traffic_symptom_exposes_observer_without_changing_rule_top5():
    class TrafficStore(TinyStore):
        edges = [{"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"}]

    event = Event("v3-e000010", 5, 8, (
        Signal("edge:5:0:traffic.dns.error_ratio", 5, "service-group:beida:dns",
               "traffic.dns.error_ratio", "dns", 1, 0, 8, "symptom"),
    ))
    pack = build_evidence(TrafficStore(), event, load_contract())
    assert pack.traffic_observations == ({
        "evidence_id": "edge:5:0:traffic.dns.error_ratio",
        "observer": "xian-traffic-vm",
        "target": "service-group:beida:dns",
    },)
    assert diagnose_rules(pack, load_contract()).roots[:3] == (
        "beida-service-vm-1", "beida-service-vm-2", "beida-service-vm-3")
    assert "xian-traffic-vm" not in diagnose_rules(pack, load_contract()).roots


def test_ospf_cost_evidence_maps_to_official_routing_category():
    event = Event("v3-e000008", 5, 12, (
        signal("guangzhou-cr-1", "ospf6_cost_anomaly", 10, "routing.ospf6_interface_cost", "cost"),
    ))
    pack = build_evidence(TinyStore(), event, load_contract())
    diagnosis = diagnose_rules(pack, load_contract())
    assert diagnosis.roots[0] == "guangzhou-cr-1"
    assert diagnosis.category == ("routing", "ospf6_cost_anomaly")


def test_elephant_loss_symptom_maps_to_link_loss_instead_of_cpu_fallback():
    event = Event("v3-e000012", 5, 9, (
        Signal("edge:6:0:traffic.elephant.loss_rate", 6,
               "service-group:beida:elephant", "traffic.elephant.loss_rate",
               "elephant", .3, .01, 9, "symptom"),
    ))
    pack = build_evidence(TinyStore(), event, load_contract())
    assert diagnose_rules(pack, load_contract()).category == ("link", "loss")


def test_auxiliary_netflow_log_and_scrape_quality_reach_evidence_pack(tmp_path):
    count = 14
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": ["xian-service-vm-1"], "edges": []},
        "features": {"node": ["node.cpu_usage", "netflow.bytes", "scrape.scrape_up"], "edge": [], "log": ["frr.bgp.err.count"]},
        "shapes": {"node": [count, 1, 3], "edge": [count, 0, 0], "log": [count, 1, 1]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    nodes = np.zeros((count, 1, 3), np.float32)
    nodes[:, 0, 0] = [10] * 5 + [78] * 3 + [10] * 6
    nodes[6, 0, 1:] = [1000, 0]
    node_mask = np.zeros_like(nodes, bool)
    node_mask[:, 0, 0] = True
    node_mask[6, 0, 1:] = True
    logs = np.zeros((count, 1, 1), np.float32)
    logs[6, 0, 0] = 1
    for kind, values, mask in (
        ("node", nodes, node_mask), ("edge", np.zeros((count, 0, 0)), np.zeros((count, 0, 0), bool)),
        ("log", logs, logs > 0),
    ):
        np.save(tmp_path / f"{kind}_values.npy", values)
        np.save(tmp_path / f"{kind}_mask.npy", mask)
    store = open_store(tmp_path)
    pack = build_evidence(store, detect(store)[0], load_contract(), include_temporal_context=True)
    assert {"netflow.bytes", "scrape.scrape_up", "frr.bgp.err.count"} <= {signal.feature for signal in pack.signals}
    assert any(signal.role == "quality" for signal in pack.signals)
    netflow = next(row for row in pack.temporal_context
                   if row["evidence_id"].endswith(":netflow.bytes"))
    assert netflow["observed_during"] == 1
    assert netflow["before_median"] is None


def test_bgp_event_includes_peer_prefix_change_as_context_candidate():
    class RoutingStore(TinyStore):
        def iter_dimension_series(self, *, source=None, node_id=None):
            assert source == "routing"
            timeline = np.arange(20, dtype=np.uint32)
            yield DimensionSeries("routing", "shanghai-br-2", "routing.bgp_peer_prefix_received",
                                  (("peer", "peer-1"),), "both", "max", timeline,
                                  np.array([4] * 5 + [1] * 5 + [4] * 10, np.float32))
            yield DimensionSeries("routing", "shanghai-br-2", "routing.bgp_peer_up",
                                  (("peer", "peer-1"),), "low", "min", timeline,
                                  np.ones(20, np.float32))

    event = Event("v3-e000009", 5, 10, (
        signal("nanjing-br-2", "bgp_session_down", 8, "routing.bgp_peer_up", "bgp"),
    ))
    pack = build_evidence(RoutingStore(), event, load_contract(), include_routing_context=True)
    assert any(item.node == "shanghai-br-2" and item.reason == "correlated_peer_prefix_change"
               for item in pack.candidates[:5])
    assert any(item.node == "shanghai-br-2" and item.role == "auxiliary"
               and item.feature == "routing.bgp_peer_prefix_received" for item in pack.signals)
    prefix = next(item for item in pack.signals if item.feature == "routing.bgp_peer_prefix_received")
    assert (prefix.value, prefix.reference) == (1, 4)
    assert diagnose_rules(pack, load_contract()).roots[0] == "nanjing-br-2"


def test_bgp_context_rejects_prefix_drop_without_peer_status_at_drop():
    class RoutingStore(TinyStore):
        def iter_dimension_series(self, *, source=None, node_id=None):
            timeline = np.arange(20, dtype=np.uint32)
            yield DimensionSeries("routing", "shanghai-br-2", "routing.bgp_peer_prefix_received",
                                  (("peer", "peer-1"),), "both", "max", timeline,
                                  np.array([4] * 5 + [1] + [4] * 14, np.float32))
            observed = np.delete(timeline, 5)
            yield DimensionSeries("routing", "shanghai-br-2", "routing.bgp_peer_up",
                                  (("peer", "peer-1"),), "low", "min", observed,
                                  np.ones(len(observed), np.float32))

    event = Event("v3-e000009", 5, 10, (
        signal("nanjing-br-2", "bgp_session_down", 8, "routing.bgp_peer_up", "bgp"),
    ))
    pack = build_evidence(RoutingStore(), event, load_contract(), include_routing_context=True)
    assert not any(item.reason == "correlated_peer_prefix_change" for item in pack.candidates)


def test_evidence_pack_contains_observed_before_during_after_values(tmp_path):
    count = 14
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": ["xian-service-vm-1"], "edges": []},
        "features": {"node": ["node.cpu_usage"], "edge": [], "log": []},
        "shapes": {"node": [count, 1, 1], "edge": [count, 0, 0], "log": [count, 1, 0]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for kind, values in (("node", np.array([10] * 5 + [78] * 3 + [10] * 6,
                                           np.float32)[:, None, None]),
                         ("edge", np.zeros((count, 0, 0))),
                         ("log", np.zeros((count, 1, 0)))):
        np.save(tmp_path / f"{kind}_values.npy", values)
        np.save(tmp_path / f"{kind}_mask.npy", np.ones_like(values, bool))
    store = open_store(tmp_path)
    pack = build_evidence(store, detect(store)[0], load_contract(), include_temporal_context=True)
    assert len(pack.temporal_context) == 1
    row = pack.temporal_context[0]
    assert (row["before_median"], row["during_median"], row["after_median"]) == (10, 78, 10)
    assert (row["observed_before"], row["observed_during"], row["observed_after"]) == (5, 3, 5)


def test_empty_temporal_context_preserves_existing_evidence_hash():
    pack = EvidencePack(
        "e1", "2026-09-17T04:00:00Z", "2026-09-17T04:02:00Z",
        (Signal("s1", 0, "xian-br-1", "node.cpu_usage", "cpu_pressure", 80, 10, 7, "direct"),),
        (Candidate("xian-br-1", 7, ("s1",), "direct_device_evidence"),),
    )
    assert pack.sha256() == "376fa894907e1997631de057b21fe82ea2153e91c2da0f5c5c979ade707e5d56"

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from aiops_v3.detection import DetectorSettings, Event, Signal, _consolidate, detect, detect_with_audit
from aiops_v3.store import open_store
from aiops_v3.data.feature_store import build_feature_store
from aiops_v3.data.observations import NumericObservation


def fixture_store(tmp_path, cpu, mask=None):
    values = np.array(cpu, np.float32)[:, None, None]
    observed = np.ones_like(values, bool) if mask is None else np.array(mask, bool)[:, None, None]
    count = len(cpu)
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": ["xian-service-vm-1"], "edges": []},
        "features": {"node": ["node.cpu_usage"], "edge": [], "log": []},
        "shapes": {"node": [count, 1, 1], "edge": [count, 0, 0], "log": [count, 1, 0]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for kind, a, m in (("node", values, observed), ("edge", np.zeros((count, 0, 0)), np.zeros((count, 0, 0), bool)), ("log", np.zeros((count, 1, 0)), np.zeros((count, 1, 0), bool))):
        np.save(tmp_path / f"{kind}_values.npy", a)
        np.save(tmp_path / f"{kind}_mask.npy", m)
    return open_store(tmp_path)


def test_sustained_cpu_jump_creates_traceable_event(tmp_path):
    store = fixture_store(tmp_path, [10] * 5 + [78] * 3 + [10] * 6)
    events = detect(store, DetectorSettings())
    assert len(events) == 1
    assert (events[0].start_minute, events[0].end_minute) == (5, 8)
    assert events[0].signals[0].node == "xian-service-vm-1"
    assert events[0].signals[0].feature == "node.cpu_usage"


def test_unobserved_high_value_never_opens_event(tmp_path):
    mask = [True] * 5 + [False] * 3 + [True] * 6
    store = fixture_store(tmp_path, [10] * 5 + [99] * 3 + [10] * 6, mask)
    assert detect(store, DetectorSettings()) == []


def test_long_single_node_episode_is_split_to_legal_windows(tmp_path):
    store = fixture_store(tmp_path, [10] * 5 + [78] * 38 + [10] * 40)
    events = detect(store)
    assert [(event.start_minute, event.end_minute) for event in events] == [(5, 35), (35, 43)]


def test_subsecond_service_latency_without_direct_fault_does_not_open_event(tmp_path):
    count = 14
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": [], "edges": [{"source": "xian-traffic-vm", "target": "service-group:wuhan:auth", "relation": "traffic"}]},
        "features": {"node": [], "edge": ["traffic.auth.latency_p95_seconds"], "log": []},
        "shapes": {"node": [count, 0, 0], "edge": [count, 1, 1], "log": [count, 0, 0]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for kind, values in (
        ("node", np.zeros((count, 0, 0))),
        ("edge", np.array([.05] * 5 + [.5] * 3 + [.05] * 6)[:, None, None]),
        ("log", np.zeros((count, 0, 0))),
    ):
        np.save(tmp_path / f"{kind}_values.npy", values)
        np.save(tmp_path / f"{kind}_mask.npy", np.ones_like(values, bool))
    assert detect(open_store(tmp_path), DetectorSettings()) == []


def test_adjacent_faults_on_distinct_nodes_remain_separate(tmp_path):
    count = 48
    values = np.full((count, 2, 1), 10, np.float32)
    values[5:24, 0, 0] = 78
    values[24:43, 1, 0] = 78
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": ["xian-service-vm-1", "xian-service-vm-2"], "edges": []},
        "features": {"node": ["node.cpu_usage"], "edge": [], "log": []},
        "shapes": {"node": [count, 2, 1], "edge": [count, 0, 0], "log": [count, 2, 0]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for kind, array in (("node", values), ("edge", np.zeros((count, 0, 0))), ("log", np.zeros((count, 2, 0)))):
        np.save(tmp_path / f"{kind}_values.npy", array)
        np.save(tmp_path / f"{kind}_mask.npy", np.ones_like(array, bool))
    events = detect(open_store(tmp_path))
    assert [(event.start_minute, event.end_minute, event.signals[0].node) for event in events] == [
        (5, 24, "xian-service-vm-1"), (24, 43, "xian-service-vm-2"),
    ]


def test_weaker_monitor_signal_inside_service_fault_is_deduplicated(tmp_path):
    count = 20
    values = np.full((count, 2, 1), 10, np.float32)
    values[5:13, 0, 0] = 80
    values[7:11, 1, 0] = 50
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": ["xian-service-vm-1", "xian-monitor-vm"], "edges": []},
        "features": {"node": ["node.cpu_usage"], "edge": [], "log": []},
        "shapes": {"node": [count, 2, 1], "edge": [count, 0, 0], "log": [count, 2, 0]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for kind, array in (("node", values), ("edge", np.zeros((count, 0, 0))), ("log", np.zeros((count, 2, 0)))):
        np.save(tmp_path / f"{kind}_values.npy", array)
        np.save(tmp_path / f"{kind}_mask.npy", np.ones_like(array, bool))
    events = detect(open_store(tmp_path))
    assert len(events) == 1
    assert events[0].signals[0].node == "xian-service-vm-1"


def multi_node_store(path, nodes, features, values):
    count = values.shape[0]
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": nodes, "edges": []},
        "features": {"node": features, "edge": [], "log": []},
        "shapes": {"node": list(values.shape), "edge": [count, 0, 0], "log": [count, len(nodes), 0]},
    }
    (path / "manifest.json").write_text(json.dumps(manifest))
    for kind, array in (("node", values), ("edge", np.zeros((count, 0, 0))),
                        ("log", np.zeros((count, len(nodes), 0)))):
        np.save(path / f"{kind}_values.npy", array)
        np.save(path / f"{kind}_mask.npy", np.ones_like(array, bool))
    return open_store(path)


def test_synchronous_same_cause_nodes_form_one_evidence_rich_event(tmp_path):
    values = np.full((20, 2, 1), 10, np.float32)
    values[5:13, :, 0] = [80, 70]
    store = multi_node_store(tmp_path, ["xian-service-vm-1", "xian-br-1"], ["node.cpu_usage"], values)
    events = detect(store)
    assert len(events) == 1
    assert (events[0].start_minute, events[0].end_minute) == (5, 13)
    assert {signal.node for signal in events[0].signals if signal.role == "direct"} == set(store.nodes)
    audit = detect_with_audit(store)
    assert audit.merged[0]["final_event_id"] == audit.events[0].event_id


def test_cross_city_synchronous_nodes_can_be_kept_separate(tmp_path):
    values = np.full((20, 2, 1), 10, np.float32)
    values[5:13, :, 0] = [80, 70]
    store = multi_node_store(tmp_path, ["xian-service-vm-1", "wuhan-br-1"],
                             ["node.cpu_usage"], values)
    assert len(detect(store)) == 1
    events = detect(store, DetectorSettings(cross_city_merge_policy="same_city"))
    assert len(events) == 2
    assert {event.signals[0].node for event in events} == set(store.nodes)


def test_synchronous_different_fault_types_stay_separate(tmp_path):
    values = np.zeros((20, 2, 2), np.float32)
    values[:, :, 0] = 10
    values[5:13, 0, 0] = 80
    values[5:13, 1, 1] = 100
    store = multi_node_store(tmp_path, ["xian-service-vm-1", "xian-service-vm-2"],
                             ["node.cpu_usage", "node.disk_io_util"], values)
    events = detect(store)
    assert len(events) == 2
    assert {signal.category for event in events for signal in event.signals if signal.role == "direct"} == {
        "cpu_pressure", "disk_io_pressure",
    }


def test_mirrored_bgp_flaps_stitch_into_one_incident(tmp_path):
    values = np.ones((22, 2, 1), np.float32)
    values[5:7, :, 0] = 0
    values[10:12, :, 0] = 0
    store = multi_node_store(tmp_path, ["xian-br-1", "beida-br-1"], ["routing.bgp_peer_up"], values)
    events = detect(store)
    assert len(events) == 1
    assert (events[0].start_minute, events[0].end_minute) == (5, 12)
    assert {signal.node for signal in events[0].signals} == set(store.nodes)


def test_exact_bgp_session_merge_is_opt_in_and_keeps_both_endpoints(monkeypatch):
    monkeypatch.setattr("aiops_v3.detection._trajectory_agrees", lambda *args: False)
    first = Event("first", 5, 12, (
        Signal("a", 5, "shenyang-br-2", "routing.bgp_peer_up",
               "bgp_session_down", 0, 1, 5, "direct"),
    ))
    second = Event("second", 5, 12, (
        Signal("b", 5, "shanghai-br-2", "routing.bgp_peer_up",
               "bgp_session_down", 0, 1, 5, "direct"),
    ))
    assert len(_consolidate(object(), [first, second], DetectorSettings())[0]) == 2
    events, merged = _consolidate(
        object(), [first, second], DetectorSettings(exact_bgp_session_merge=True)
    )
    assert len(events) == 1
    assert {signal.node for signal in events[0].signals} == {
        "shenyang-br-2", "shanghai-br-2",
    }
    assert merged[0]["reason"] == "same_window_bgp_peer"
    shifted = Event("shifted", 6, 12, second.signals)
    assert len(_consolidate(
        object(), [first, shifted], DetectorSettings(exact_bgp_session_merge=True)
    )[0]) == 2


def test_same_minute_but_different_cpu_trajectories_stay_separate(tmp_path):
    values = np.full((24, 2, 1), 10, np.float32)
    values[5:13, 0, 0] = 80
    values[5:7, 1, 0] = 70
    store = multi_node_store(tmp_path, ["xian-service-vm-1", "beida-service-vm-1"],
                             ["node.cpu_usage"], values)
    events = detect(store)
    assert len(events) == 2


def test_sparse_bgp_flaps_do_not_form_unmatchably_long_interval(tmp_path):
    values = np.ones((28, 1, 1), np.float32)
    values[5:7, 0, 0] = 0
    values[10:12, 0, 0] = 0
    values[15:17, 0, 0] = 0
    store = multi_node_store(tmp_path, ["xian-br-1"], ["routing.bgp_peer_up"], values)
    events = detect(store)
    assert [(event.start_minute, event.end_minute) for event in events] == [(5, 12), (15, 17)]


def test_conservative_profile_audits_short_weak_cpu_only_event(tmp_path):
    store = fixture_store(tmp_path, [10] * 5 + [35] * 3 + [10] * 8)
    assert len(detect(store)) == 1
    result = detect_with_audit(store, DetectorSettings(weak_cpu_max_minutes=3, weak_cpu_max_score=8))
    assert result.events == ()
    assert [item["reason"] for item in result.rejected] == ["short_weak_single_cpu_signal"]


def test_conservative_profile_keeps_sustained_cpu_incident(tmp_path):
    store = fixture_store(tmp_path, [10] * 5 + [78] * 7 + [10] * 10)
    result = detect_with_audit(store, DetectorSettings(weak_cpu_max_minutes=3, weak_cpu_max_score=8))
    assert [(event.start_minute, event.end_minute) for event in result.events] == [(5, 12)]


def test_sparse_sample_baseline_recovers_fault_longer_than_healthy_context(tmp_path):
    cpu = [10] * 5 + [78] * 13 + [10] * 5 + [0] * 17
    observed = [True] * 23 + [False] * 17
    store = fixture_store(tmp_path, cpu, observed)
    events = detect(store)
    assert [(event.start_minute, event.end_minute) for event in events] == [(5, 18)]


def test_request_aware_service_detection_rejects_one_request_spike_but_keeps_supported_failure(tmp_path):
    def service_store(path, request_rate):
        path.mkdir()
        count = 14
        manifest = {
            "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
            "entities": {"nodes": [], "edges": [{"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"}]},
            "features": {"node": [], "edge": ["traffic.dns.error_ratio", "traffic.dns.requests_rate"], "log": []},
            "shapes": {"node": [count, 0, 0], "edge": [count, 1, 2], "log": [count, 0, 0]},
        }
        (path / "manifest.json").write_text(json.dumps(manifest))
        edge = np.zeros((count, 1, 2), np.float32)
        edge[:, 0, 1] = request_rate
        edge[5:8, 0, 0] = 1
        for kind, values in (("node", np.zeros((count, 0, 0))), ("edge", edge),
                             ("log", np.zeros((count, 0, 0)))):
            np.save(path / f"{kind}_values.npy", values)
            np.save(path / f"{kind}_mask.npy", np.ones_like(values, bool))
        return open_store(path)

    low = service_store(tmp_path / "low", 1 / 60)
    high = service_store(tmp_path / "high", 60)
    assert len(detect(low)) == 1
    settings = DetectorSettings(request_aware_service=True)
    assert detect(low, settings) == []
    assert len(detect(high, settings)) == 1


def test_sustained_ospf_interface_cost_jump_opens_routing_event(tmp_path):
    values = np.ones((25, 1, 1), np.float32)
    values[8:15, 0, 0] = 100
    store = multi_node_store(tmp_path, ["guangzhou-cr-1"], ["routing.ospf6_interface_cost"], values)
    events = detect(store)
    assert [(event.start_minute, event.end_minute) for event in events] == [(8, 15)]
    assert events[0].signals[0].category == "ospf6_cost_anomaly"


def test_unrelated_router_cpu_does_not_hide_same_city_service_fault(tmp_path):
    count = 30
    node = np.full((count, 1, 1), 10, np.float32)
    node[8:14, 0, 0] = 75
    edge = np.zeros((count, 1, 1), np.float32)
    edge[9:14, 0, 0] = .9
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": ["beida-br-1"], "edges": [
            {"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"}]},
        "features": {"node": ["node.cpu_usage"], "edge": ["traffic.dns.error_ratio"], "log": []},
        "shapes": {"node": list(node.shape), "edge": list(edge.shape), "log": [count, 1, 0]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for kind, values in (("node", node), ("edge", edge), ("log", np.zeros((count, 1, 0)))):
        np.save(tmp_path / f"{kind}_values.npy", values)
        np.save(tmp_path / f"{kind}_mask.npy", np.ones_like(values, bool))
    result = detect_with_audit(open_store(tmp_path), DetectorSettings(service_overlap_policy="related_device"))
    assert len(result.events) == 2
    assert {signal.category for event in result.events for signal in event.signals} >= {"cpu_pressure", "dns"}


def test_healthy_tail_baseline_recovers_long_fault_hidden_by_global_median(tmp_path):
    store = fixture_store(tmp_path, [10] * 30 + [75] * 40)
    assert detect(store) == []
    events = detect(store, DetectorSettings(baseline_strategy="healthy_tail"))
    assert [(event.start_minute, event.end_minute) for event in events] == [(30, 60), (60, 70)]


def test_rolling_healthy_tail_uses_prior_observations_for_long_fault(tmp_path):
    store = fixture_store(tmp_path, [10] * 30 + [75] * 40)
    events = detect(store, DetectorSettings(baseline_strategy="rolling_healthy_tail"))
    assert [(event.start_minute, event.end_minute) for event in events] == [(30, 60), (60, 70)]
    assert all(signal.reference == 10 for event in events for signal in event.signals)


def test_rolling_healthy_tail_needs_prior_observations(tmp_path):
    store = fixture_store(tmp_path, [75] * 10 + [10] * 60)
    assert detect(store, DetectorSettings(baseline_strategy="rolling_healthy_tail")) == []


def test_stable_bgp_peer_with_correlated_route_loss_opens_filter_candidate(tmp_path):
    t0 = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    rows = []
    for minute in range(20):
        for metric, value, dimensions, direction in (
            ("routing.bgp_peer_prefix_received", 1 if 6 <= minute < 11 else 4,
             (("peer", "p1"),), "both"),
            ("routing.bgp_peer_up", 1, (("peer", "p1"),), "low"),
            ("routing.ipv6_route_count", 12 if 6 <= minute < 11 else 15,
             (("protocol", "all"),), "both"),
        ):
            rows.append(NumericObservation(t0 + timedelta(minutes=minute), "routing", "xian-br-1",
                                           (), metric, value, dimensions, direction))
    network = json.loads((Path(__file__).parents[2] / "aiops_v3/config/network_elements.json").read_text())
    store = open_store(build_feature_store(rows, network, tmp_path / "canonical").path)
    assert detect(store) == []
    events = detect(store, DetectorSettings(routing_dimension_detection=True))
    assert [(event.start_minute, event.end_minute) for event in events] == [(6, 11)]
    assert {signal.category for signal in events[0].signals} == {"bgp_route_filter"}
    assert {signal.feature for signal in events[0].signals} == {
        "routing.bgp_peer_prefix_received", "routing.ipv6_route_count"}


def test_routing_gap_never_emits_nan_dimension_signal(tmp_path):
    t0 = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    rows = []
    for minute in range(20):
        metrics = [
            ("routing.bgp_peer_up", 1, (("peer", "p1"),), "low"),
            ("routing.ipv6_route_count", 12 if 6 <= minute < 11 else 15,
             (("protocol", "all"),), "both"),
        ]
        if minute != 8:
            metrics.append(("routing.bgp_peer_prefix_received", 1 if 6 <= minute < 11 else 4,
                            (("peer", "p1"),), "both"))
        for metric, value, dimensions, direction in metrics:
            rows.append(NumericObservation(t0 + timedelta(minutes=minute), "routing", "xian-br-1",
                                           (), metric, value, dimensions, direction))
    network = json.loads((Path(__file__).parents[2] / "aiops_v3/config/network_elements.json").read_text())
    store = open_store(build_feature_store(rows, network, tmp_path / "canonical").path)
    events = detect(store, DetectorSettings(routing_dimension_detection=True))
    assert [(event.start_minute, event.end_minute) for event in events] == [(6, 11)]
    assert all(np.isfinite(signal.value) and np.isfinite(signal.reference)
               for event in events for signal in event.signals)


def test_two_affected_peers_on_one_router_form_one_filter_event(tmp_path):
    t0 = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    rows = []
    for minute in range(20):
        for peer in ("p1", "p2"):
            for metric, value, direction in (
                ("routing.bgp_peer_prefix_received", 1 if 6 <= minute < 11 else 4, "both"),
                ("routing.bgp_peer_up", 1, "low"),
            ):
                rows.append(NumericObservation(t0 + timedelta(minutes=minute), "routing", "xian-br-1",
                                               (), metric, value, (("peer", peer),), direction))
        rows.append(NumericObservation(t0 + timedelta(minutes=minute), "routing", "xian-br-1",
                                       (), "routing.ipv6_route_count", 12 if 6 <= minute < 11 else 15,
                                       (("protocol", "all"),), "both"))
    network = json.loads((Path(__file__).parents[2] / "aiops_v3/config/network_elements.json").read_text())
    store = open_store(build_feature_store(rows, network, tmp_path / "canonical").path)
    events = detect(store, DetectorSettings(routing_dimension_detection=True))
    assert len(events) == 1
    assert sum(s.feature == "routing.bgp_peer_prefix_received" for s in events[0].signals) == 2


def test_unrelated_down_peer_does_not_hide_stable_peer_route_loss(tmp_path):
    t0 = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    rows = []
    for minute in range(20):
        for metric, value, dimensions, direction in (
            ("routing.bgp_peer_prefix_received", 1 if 6 <= minute < 11 else 4,
             (("peer", "p1"),), "both"),
            ("routing.bgp_peer_up", 1, (("peer", "p1"),), "low"),
            ("routing.bgp_peer_up", 0 if 6 <= minute < 11 else 1,
             (("peer", "p2"),), "low"),
            ("routing.ipv6_route_count", 12 if 6 <= minute < 11 else 15,
             (("protocol", "all"),), "both"),
        ):
            rows.append(NumericObservation(t0 + timedelta(minutes=minute), "routing", "xian-br-1",
                                           (), metric, value, dimensions, direction))
    network = json.loads((Path(__file__).parents[2] / "aiops_v3/config/network_elements.json").read_text())
    store = open_store(build_feature_store(rows, network, tmp_path / "canonical").path)
    events = detect(store, DetectorSettings(routing_dimension_detection=True))
    assert any(signal.category == "bgp_route_filter"
               for event in events for signal in event.signals)


def test_ospf_interface_state_flap_with_neighbor_loss_has_one_specific_event(tmp_path):
    t0 = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    rows = []
    for minute in range(20):
        for metric, value, dimensions, direction in (
            ("routing.ospf6_interface_enabled", 0 if 6 <= minute < 9 else 1,
             (("interface", "ens5"),), "low"),
            ("routing.ospf6_neighbor_state_code", 1 if 6 <= minute < 9 else 6,
             (("interface", "ens5"), ("neighbor_id", "n1")), "low"),
        ):
            rows.append(NumericObservation(t0 + timedelta(minutes=minute), "routing", "xian-cr-1",
                                           (), metric, value, dimensions, direction))
    network = json.loads((Path(__file__).parents[2] / "aiops_v3/config/network_elements.json").read_text())
    store = open_store(build_feature_store(rows, network, tmp_path / "canonical").path)
    events = detect(store, DetectorSettings(routing_dimension_detection=True))
    assert len(events) == 1
    assert (events[0].start_minute, events[0].end_minute) == (6, 9)
    assert {signal.category for signal in events[0].signals} == {"ospf6_interface_flap"}


def test_ospf_interface_flap_requires_neighbor_transition(tmp_path):
    t0 = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    rows = []
    for minute in range(20):
        for metric, value, dimensions in (
            ("routing.ospf6_interface_enabled", 0 if 6 <= minute < 9 else 1,
             (("interface", "ens5"),)),
            ("routing.ospf6_neighbor_state_code", 1,
             (("interface", "ens5"), ("neighbor_id", "n1"))),
        ):
            rows.append(NumericObservation(t0 + timedelta(minutes=minute), "routing", "xian-cr-1",
                                           (), metric, value, dimensions, "low"))
    network = json.loads((Path(__file__).parents[2] / "aiops_v3/config/network_elements.json").read_text())
    store = open_store(build_feature_store(rows, network, tmp_path / "canonical").path)
    assert detect(store, DetectorSettings(routing_dimension_detection=True)) == []


def test_stable_peer_prefix_loss_with_route_change_counter_is_candidate(tmp_path):
    t0 = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    rows = []
    for minute in range(20):
        for metric, value, dimensions, direction in (
            ("routing.bgp_peer_prefix_received", 1 if 6 <= minute < 11 else 4,
             (("peer", "p1"),), "both"),
            ("routing.bgp_peer_up", 1, (("peer", "p1"),), "low"),
            ("routing.ipv6_route_change_total", 1 if minute >= 6 else 0,
             (("prefix", "fd00:1::/64"),), "both"),
        ):
            rows.append(NumericObservation(t0 + timedelta(minutes=minute), "routing", "xian-br-1",
                                           (), metric, value, dimensions, direction))
    network = json.loads((Path(__file__).parents[2] / "aiops_v3/config/network_elements.json").read_text())
    store = open_store(build_feature_store(rows, network, tmp_path / "canonical").path)
    events = detect(store, DetectorSettings(routing_dimension_detection=True))
    assert [(e.start_minute, e.end_minute) for e in events] == [(6, 11)]
    assert {s.feature for s in events[0].signals} == {
        "routing.bgp_peer_prefix_received", "routing.ipv6_route_change_total"}


def test_route_change_support_at_first_window_is_not_lost(tmp_path):
    t0 = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    rows = []
    for minute in range(20):
        for metric, value, dimensions, direction in (
            ("routing.bgp_peer_prefix_received", 1 if minute < 5 else 4,
             (("peer", "p1"),), "both"),
            ("routing.bgp_peer_up", 1, (("peer", "p1"),), "low"),
            ("routing.ipv6_route_change_total", 1 if minute >= 1 else 0,
             (("prefix", "fd00:1::/64"),), "both"),
        ):
            rows.append(NumericObservation(t0 + timedelta(minutes=minute), "routing", "xian-br-1",
                                           (), metric, value, dimensions, direction))
    network = json.loads((Path(__file__).parents[2] / "aiops_v3/config/network_elements.json").read_text())
    store = open_store(build_feature_store(rows, network, tmp_path / "canonical").path)
    events = detect(store, DetectorSettings(routing_dimension_detection=True))
    assert [(e.start_minute, e.end_minute) for e in events] == [(0, 5)]

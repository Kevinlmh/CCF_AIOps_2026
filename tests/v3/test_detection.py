import json

import numpy as np

from aiops_v3.detection import DetectorSettings, detect, detect_with_audit
from aiops_v3.store import open_store


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

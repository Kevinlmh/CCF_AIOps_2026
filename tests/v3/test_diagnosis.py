from datetime import datetime, timezone
import json

import numpy as np

from aiops_v3.contracts import load_contract
from aiops_v3.detection import Event, Signal
from aiops_v3.detection import detect
from aiops_v3.diagnosis import build_evidence, diagnose_rules
from aiops_v3.store import open_store


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
    pack = build_evidence(store, detect(store)[0], load_contract())
    assert {"netflow.bytes", "scrape.scrape_up", "frr.bgp.err.count"} <= {signal.feature for signal in pack.signals}
    assert any(signal.role == "quality" for signal in pack.signals)

from datetime import datetime, timezone

from aiops_v3.contracts import load_contract
from aiops_v3.detection import Event, Signal
from aiops_v3.diagnosis import build_evidence, diagnose_rules


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

from aiops_v3.contracts import load_contract
from aiops_v3.detection import Event, Signal
from aiops_v3.discovery import ClusterBundle, CandidateWindow, validate_discovery


def bundle():
    signals = (
        Signal("node:5:0:node.cpu_usage", 104, "xian-service-vm-1", "node.cpu_usage",
               "cpu_pressure", 80, 10, 7, "direct"),
        Signal("edge:5:1:traffic.dns.error_ratio", 105, "service-group:beida:dns",
               "traffic.dns.error_ratio", "dns", 1, 0, 8, "symptom"),
    )
    windows = (CandidateWindow(103, 108, ("rule",), signals),)
    owners = {"node:5:0:node.cpu_usage": "xian-service-vm-1",
              "edge:5:1:traffic.dns.error_ratio": "beida-service-vm-1"}
    return ClusterBundle(
        cluster_id="c000001", start_minute=100, end_minute=110,
        windows=windows, rule_events=(), material={}, signal_index={s.evidence_id: s for s in signals},
        evidence_owners=owners, candidate_nodes=("xian-service-vm-1", "beida-service-vm-1"),
        start_time="2026-09-17T05:40:00Z", end_time="2026-09-17T05:50:00Z",
    )


def body(**overrides):
    event = {
        "start_time": "2026-09-17T05:44:00Z", "end_time": "2026-09-17T05:49:00Z",
        "root_cause_top5": [{"rank": 1, "network_element_id": "xian-service-vm-1",
                            "evidence_ids": ["node:5:0:node.cpu_usage"]}],
        "major_category": "resource", "sub_category": "cpu_pressure",
        "mechanism_evidence_ids": ["node:5:0:node.cpu_usage"],
        "observation_note": "cpu rise",
    }
    event.update(overrides)
    return {"events": [event], "rejected_reason": ""}


def test_valid_discovery_event_passes():
    assert validate_discovery(bundle(), body(), load_contract()) is None


def test_cross_device_root_evidence_is_rejected():
    payload = body(root_cause_top5=[{"rank": 1, "network_element_id": "beida-service-vm-1",
                                     "evidence_ids": ["node:5:0:node.cpu_usage"]}])
    assert validate_discovery(bundle(), payload, load_contract()) == "cross_device_evidence"


def test_unknown_evidence_and_bad_boundary_are_rejected():
    payload = body(mechanism_evidence_ids=["missing"], )
    assert validate_discovery(bundle(), payload, load_contract()) == "unknown_evidence"
    payload = body(start_time="2026-09-17T05:20:00Z", end_time="2026-09-17T05:49:00Z")
    assert validate_discovery(bundle(), payload, load_contract()) == "boundary_invalid"


def test_category_mechanism_mismatch_is_rejected():
    payload = body(major_category="link", sub_category="loss", mechanism_evidence_ids=[
        "node:5:0:node.cpu_usage"])
    assert validate_discovery(bundle(), payload, load_contract()) == "mechanism_missing"


def test_overlapping_events_are_rejected():
    payload = body()
    payload["events"].append(dict(payload["events"][0],
                                  start_time="2026-09-17T05:48:00Z",
                                  end_time="2026-09-17T05:52:00Z"))
    assert validate_discovery(bundle(), payload, load_contract()) == "events_overlap"


def test_empty_events_is_valid():
    assert validate_discovery(bundle(), {"events": [], "rejected_reason": "no mechanism"},
                              load_contract()) is None

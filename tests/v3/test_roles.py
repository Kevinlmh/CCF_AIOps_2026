import json

from aiops_v3.diagnosis import Candidate, Diagnosis, EvidencePack
from aiops_v3.detection import Signal
from aiops_v3.roles import build_request, validate


def pack(*, signals=None, candidates=None, observers=(), owners=None, quality=None,
         start="2026-09-17T04:10:00Z", end="2026-09-17T04:20:00Z"):
    signals = signals or (
        Signal("node:10:0:node.cpu_usage", 10, "beida-service-vm-1", "node.cpu_usage",
               "cpu_pressure", 80, 10, 8, "direct"),
        Signal("node:10:1:node.cpu_usage", 10, "xian-br-1", "node.cpu_usage",
               "cpu_pressure", 70, 10, 6, "direct"),
    )
    candidates = candidates or (
        Candidate("beida-service-vm-1", 8, ("node:10:0:node.cpu_usage",), "direct_device_evidence"),
        Candidate("xian-br-1", 6, ("node:10:1:node.cpu_usage",), "direct_device_evidence"),
        Candidate("xian-service-vm-1", 0.3, (), "same_city_context_no_direct_evidence"),
    )
    owners = owners if owners is not None else {item.evidence_id: item.node for item in signals}
    return EvidencePack(
        "v3-e000001", start, end, tuple(signals), tuple(candidates),
        observers=tuple(observers), evidence_owners=owners, quality=quality or {},
        candidate_priors={item.node: item.reason for item in candidates},
    )


def baseline():
    return Diagnosis(("beida-service-vm-1", "xian-br-1", "xian-service-vm-1", "beida-br-1", "beida-fw"),
                     ("resource", "cpu_pressure"), ())


def test_confirm_accepts_small_boundary_shift():
    p = pack()
    body = {
        "observations": ["cpu high"], "support_evidence_ids": ["node:10:0:node.cpu_usage"],
        "counter_evidence_ids": [], "missing": [], "verdict": "supported",
        "boundary": {"start_time": "2026-09-17T04:12:00Z", "end_time": "2026-09-17T04:19:00Z"},
    }
    assert validate("confirm", p, body, baseline=baseline(), upstream={}) is None


def test_confirm_rejects_out_of_range_boundary():
    p = pack()
    for start, end in (("2026-09-17T03:00:00Z", "2026-09-17T04:20:00Z"),   # shift too large
                       ("2026-09-17T04:19:00Z", "2026-09-17T04:20:00Z")):   # dice too low
        body = {
            "observations": [], "support_evidence_ids": [], "counter_evidence_ids": [],
            "missing": [], "verdict": "supported",
            "boundary": {"start_time": start, "end_time": end},
        }
        assert validate("confirm", p, body, baseline=baseline(), upstream={}) == "boundary_invalid"


def test_confirm_uncertain_requires_null_boundary():
    p = pack()
    body = {"observations": [], "support_evidence_ids": [], "counter_evidence_ids": [],
            "missing": [], "verdict": "uncertain", "boundary": None}
    assert validate("confirm", p, body, baseline=baseline(), upstream={}) is None
    body["boundary"] = {"start_time": "2026-09-17T04:11:00Z", "end_time": "2026-09-17T04:19:00Z"}
    assert validate("confirm", p, body, baseline=baseline(), upstream={}) == "boundary_invalid"


def test_localize_rejects_cross_device_citation():
    p = pack()
    body = {
        "observations": [], "counter_evidence_ids": [],
        "ranked": [{"rank": 1, "network_element_id": "xian-br-1",
                    "evidence_ids": ["node:10:0:node.cpu_usage"], "mechanism": "cpu"}],
    }
    assert validate("localize", p, body, baseline=baseline(), upstream={}) == "cross_device_evidence"


def test_localize_rejects_bad_rank_sequence():
    p = pack()
    body = {
        "observations": [], "counter_evidence_ids": [],
        "ranked": [{"rank": 2, "network_element_id": "beida-service-vm-1",
                    "evidence_ids": ["node:10:0:node.cpu_usage"], "mechanism": "cpu"}],
    }
    assert validate("localize", p, body, baseline=baseline(), upstream={}) == "rank_invalid"


def test_localize_accepts_valid_ranking_with_direct_rank1():
    p = pack()
    body = {
        "observations": [], "counter_evidence_ids": [],
        "ranked": [
            {"rank": 1, "network_element_id": "beida-service-vm-1",
             "evidence_ids": ["node:10:0:node.cpu_usage"], "mechanism": "cpu"},
            {"rank": 2, "network_element_id": "xian-br-1",
             "evidence_ids": ["node:10:1:node.cpu_usage"], "mechanism": "cpu"},
        ],
    }
    assert validate("localize", p, body, baseline=baseline(), upstream={}) is None


def test_classify_rejects_traffic_mechanism_for_cpu():
    p = pack(signals=(
        Signal("edge:10:0:traffic.dns.error_ratio", 10, "beida-service-vm-1",
               "traffic.dns.error_ratio", "dns", 1, 0, 8, "symptom"),
    ))
    body = {
        "observations": [], "mechanism_evidence_ids": ["edge:10:0:traffic.dns.error_ratio"],
        "counter_evidence_ids": [], "major_category": "resource", "sub_category": "cpu_pressure",
    }
    assert validate("classify", p, body, baseline=baseline(), upstream={}) == "mechanism_missing"


def test_classify_accepts_matching_resource_mechanism():
    p = pack()
    body = {
        "observations": [], "mechanism_evidence_ids": ["node:10:0:node.cpu_usage"],
        "counter_evidence_ids": [], "major_category": "resource", "sub_category": "cpu_pressure",
    }
    assert validate("classify", p, body, baseline=baseline(), upstream={}) is None


def test_review_requires_upstream_and_counter_evidence():
    p = pack()
    confirm = {"observations": [], "support_evidence_ids": [], "counter_evidence_ids": [],
               "missing": [], "verdict": "uncertain", "boundary": None}
    localize = {"observations": [], "counter_evidence_ids": [],
                "ranked": [{"rank": 1, "network_element_id": "beida-service-vm-1",
                            "evidence_ids": ["node:10:0:node.cpu_usage"], "mechanism": "cpu"}]}
    classify = {"observations": [], "mechanism_evidence_ids": ["node:10:0:node.cpu_usage"],
                "counter_evidence_ids": [], "major_category": "resource", "sub_category": "cpu_pressure"}
    upstream = {"confirm": confirm, "localize": localize, "classify": classify}
    body = {"notes": "ok", "support_evidence_ids": [], "counter_evidence_ids": [],
            "confirm": "accept", "localize": "accept", "classify": "accept"}
    assert validate("review", p, body, baseline=baseline(), upstream=upstream) is None
    reject = dict(body, classify="reject")
    assert validate("review", p, reject, baseline=baseline(),
                    upstream=upstream) == "counter_evidence_required"
    missing = dict(body, support_evidence_ids=[])
    assert validate("review", p, missing, baseline=baseline(), upstream={}) == "missing_upstream"


def test_prompt_is_evidence_first_and_names_observer_rule():
    p = pack()
    request = build_request("confirm", p, baseline=baseline(), upstream={})
    system = request["messages"][0]["content"].lower()
    assert "observer" in system and "topology" in system and "evidence" in system
    material = json.loads(request["messages"][1]["content"])
    keys = list(material)
    assert keys.index("observations") < keys.index("rule_baseline")

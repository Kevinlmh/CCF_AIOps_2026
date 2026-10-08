from dataclasses import replace

from aiops_v3.contracts import load_contract
from aiops_v3.diagnosis import Candidate, EvidencePack, diagnose_rules
from aiops_v3.detection import Signal
from aiops_v3.optional_llm import review_diagnosis


def fixture():
    nodes = ["xian-service-vm-1", "xian-service-vm-2", "xian-service-vm-3",
             "xian-br-1", "xian-br-2", "xian-traffic-vm"]
    signals = (Signal("cpu1", 5, nodes[0], "node.cpu_usage", "cpu_pressure", 80, 10, 10, "direct"),
               Signal("process2", 5, nodes[1], "node.process_count", "process_pressure", 400, 40, 8, "direct"),
               Signal("symptom", 5, "service-group:xian:web", "traffic.web.error_ratio", "web", 1, 0, 10, "symptom"))
    pack = EvidencePack("event1", "2026-09-17T04:05:00Z", "2026-09-17T04:08:00Z",
                        signals, tuple(Candidate(n, 10-i, (), "test") for i,n in enumerate(nodes)))
    rule = diagnose_rules(pack, load_contract())
    response = {"event_id": pack.event_id, "evidence_sha256": pack.sha256(),
                "root_cause_top5": list(rule.roots),
                "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"},
                "evidence_ids": ["cpu1"]}
    return pack, rule, response


def test_observer_cannot_be_promoted_by_its_business_symptom():
    pack, rule, response = fixture()
    response["root_cause_top5"] = ["xian-traffic-vm", *rule.roots[:4]]
    response["evidence_ids"] = ["symptom"]
    review = review_diagnosis(pack, rule, response, load_contract(), policy="evidence-gated")
    assert review.diagnosis.roots == rule.roots
    assert review.root_status == "unsupported_root_change"


def test_unrelated_category_rejected_independently_of_supported_root_change():
    pack, rule, response = fixture()
    response["root_cause_top5"][:2] = reversed(response["root_cause_top5"][:2])
    response["fault_category"]["sub_category"] = "disk_space_low"
    response["evidence_ids"] = ["process2"]
    review = review_diagnosis(pack, rule, response, load_contract(), policy="evidence-gated")
    assert review.diagnosis.roots[0] == "xian-service-vm-2"
    assert review.diagnosis.category == rule.category
    assert review.category_status == "unsupported_category_change"


def test_field_ablations_and_staged_default_gate():
    pack, rule, response = fixture()
    response["root_cause_top5"][:2] = reversed(response["root_cause_top5"][:2])
    response["fault_category"]["sub_category"] = "process_pressure"
    response["evidence_ids"] = ["process2"]
    both = review_diagnosis(pack, rule, response, load_contract(), policy="evidence-gated")
    assert both.reason is None
    assert both.diagnosis.category == ("resource", "process_pressure")
    root_only = review_diagnosis(pack, rule, response, load_contract(), policy="evidence-gated", application="roots-only")
    assert root_only.diagnosis.category == rule.category
    cat_only = review_diagnosis(pack, rule, response, load_contract(), policy="evidence-gated", application="category-only")
    assert cat_only.diagnosis.roots == rule.roots
    assert cat_only.diagnosis.category == rule.category  # process evidence is on a different root
    response.update(workflow="staged", root_evidence_ids=["symptom"], category_evidence_ids=["process2"])
    staged = review_diagnosis(pack, rule, response, load_contract())
    assert staged.diagnosis.roots == rule.roots


def test_context_change_is_citable_but_normal_metric_is_not_root_evidence():
    pack, rule, response = fixture()
    pack = replace(pack, device_context=({"node": "xian-traffic-vm", "metrics": [
        {"evidence_id": "context1", "feature": "node.cpu_usage", "supported_category": "cpu_pressure"}]},))
    response["evidence_sha256"] = pack.sha256()
    response["root_cause_top5"] = ["xian-traffic-vm", *rule.roots[:4]]
    response["evidence_ids"] = ["context1"]
    assert review_diagnosis(pack, rule, response, load_contract(), policy="evidence-gated").reason is None

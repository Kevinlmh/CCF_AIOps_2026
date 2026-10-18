from datetime import datetime, timezone

from aiops_v3.contracts import load_contract
from aiops_v3.detection import Event, Signal
from aiops_v3.diagnosis import build_evidence, diagnose_rules
from aiops_v3.role_import import apply_roles


class ClockStore:
    def time_at(self, minute):
        return datetime(2026, 9, 17, 4, minute, tzinfo=timezone.utc)


def setup_pack():
    event = Event("v3-e000001", 5, 8, (
        Signal("node:5:0:node.cpu_usage", 5, "xian-service-vm-1", "node.cpu_usage",
               "cpu_pressure", 80, 10, 7, "direct"),
        Signal("node:5:1:node.cpu_usage", 5, "xian-br-1", "node.cpu_usage",
               "cpu_pressure", 70, 10, 6, "direct"),
    ))
    pack = build_evidence(ClockStore(), event, load_contract())
    rules = diagnose_rules(pack, load_contract())
    return pack, rules


def bodies(pack):
    cpu_id = "node:5:0:node.cpu_usage"
    br_id = "node:5:1:node.cpu_usage"
    return {
        "confirm": {"observations": [], "support_evidence_ids": [cpu_id],
                    "counter_evidence_ids": [], "missing": [], "verdict": "uncertain",
                    "boundary": None},
        "localize": {"observations": [], "counter_evidence_ids": [],
                     "ranked": [
                         {"rank": 1, "network_element_id": "xian-br-1",
                          "evidence_ids": [br_id], "mechanism": "cpu"},
                         {"rank": 2, "network_element_id": "xian-service-vm-1",
                          "evidence_ids": [cpu_id], "mechanism": "cpu"},
                     ]},
        "classify": {"observations": [], "mechanism_evidence_ids": [br_id],
                     "counter_evidence_ids": [], "major_category": "resource",
                     "sub_category": "cpu_pressure"},
        "review": {"notes": "ok", "support_evidence_ids": [br_id], "counter_evidence_ids": [],
                   "confirm": "accept", "localize": "accept", "classify": "accept"},
    }


def test_roles_change_roots_but_keep_event_window():
    pack, rules = setup_pack()
    diagnosis, boundary, fallbacks = apply_roles(pack, rules, bodies(pack), load_contract())
    assert diagnosis.roots[0] == "xian-br-1"
    assert diagnosis.roots[1] == "xian-service-vm-1"
    assert boundary is None
    assert fallbacks == []
    assert "roots" not in fallbacks


def test_invalid_category_falls_back_to_rules():
    pack, rules = setup_pack()
    rows = bodies(pack)
    rows["classify"].update({"major_category": "link", "sub_category": "loss"})
    diagnosis, _, fallbacks = apply_roles(pack, rules, rows, load_contract())
    assert diagnosis.category == rules.category
    assert "category" in fallbacks


def test_missing_review_does_not_block_validated_fields():
    pack, rules = setup_pack()
    rows = bodies(pack)
    rows.pop("review")
    diagnosis, _, fallbacks = apply_roles(pack, rules, rows, load_contract())
    assert diagnosis.roots[0] == "xian-br-1"
    assert fallbacks == []


def test_explicit_review_reject_vetoes_field():
    pack, rules = setup_pack()
    rows = bodies(pack)
    rows["review"].update({"localize": "reject", "counter_evidence_ids": ["node:5:1:node.cpu_usage"]})
    diagnosis, _, fallbacks = apply_roles(pack, rules, rows, load_contract())
    assert diagnosis.roots == rules.roots
    assert "roots" in fallbacks


def test_missing_roles_keep_rule_diagnosis():
    pack, rules = setup_pack()
    diagnosis, boundary, fallbacks = apply_roles(pack, rules, {}, load_contract())
    assert diagnosis.roots == rules.roots
    assert diagnosis.category == rules.category
    assert boundary is None
    assert set(fallbacks) == {"roots", "category", "boundary"}

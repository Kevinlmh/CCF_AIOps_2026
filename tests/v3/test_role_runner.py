import json
from dataclasses import asdict
from datetime import datetime, timezone

from aiops_v3.contracts import load_contract
from aiops_v3.detection import Event, Signal
from aiops_v3.diagnosis import build_evidence
from aiops_v3.role_runner import run_roles


class ClockStore:
    def time_at(self, minute):
        return datetime(2026, 9, 17, 4, minute, tzinfo=timezone.utc)


def record_and_pack():
    event = Event("v3-e000001", 5, 8, (
        Signal("node:5:0:node.cpu_usage", 5, "xian-service-vm-1", "node.cpu_usage",
               "cpu_pressure", 80, 10, 7, "direct"),
    ))
    pack = build_evidence(ClockStore(), event, load_contract())
    return {**asdict(pack), "evidence_sha256": pack.sha256()}, pack


def body_for(pack, schema_name):
    signal_id = pack.signals[0].evidence_id
    candidate = pack.candidates[0]
    if schema_name == "v3_role_confirm":
        return {"observations": ["cpu high"], "support_evidence_ids": [signal_id],
                "counter_evidence_ids": [], "missing": [], "verdict": "uncertain",
                "boundary": None}
    if schema_name == "v3_role_localize":
        return {"observations": [], "counter_evidence_ids": [],
                "ranked": [{"rank": 1, "network_element_id": candidate.node,
                            "evidence_ids": list(candidate.evidence_ids), "mechanism": "cpu"}]}
    if schema_name == "v3_role_classify":
        return {"observations": [], "mechanism_evidence_ids": [signal_id],
                "counter_evidence_ids": [], "major_category": "resource",
                "sub_category": "cpu_pressure"}
    return {"notes": "ok", "support_evidence_ids": [signal_id], "counter_evidence_ids": [],
            "confirm": "accept", "localize": "accept", "classify": "accept"}


def fake_complete(calls):
    def complete(payload):
        calls.append(payload)
        name = payload["response_format"]["json_schema"]["name"]
        pack = record_and_pack()[1]
        return {"choices": [{"message": {"content": json.dumps(body_for(pack, name))}}]}
    return complete


def test_runner_accepts_four_roles_and_resumes(tmp_path):
    record, _ = record_and_pack()
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text(json.dumps(record) + "\n")
    calls = []
    summary = run_roles(evidence, tmp_path / "roles", model="m", revision="r",
                        base_url="http://x", complete=fake_complete(calls))
    assert {role: summary["roles"][role]["accepted"] for role in summary["roles"]} == {
        "confirm": 1, "localize": 1, "classify": 1, "review": 1}
    assert len(calls) == 4
    rows = [json.loads(line) for line in (tmp_path / "roles" / "roles.jsonl").read_text().splitlines()]
    assert {row["role"] for row in rows} == {"confirm", "localize", "classify", "review"}
    assert all(row["status"] == "accepted" for row in rows)

    calls.clear()
    again = run_roles(evidence, tmp_path / "roles", model="m", revision="r",
                      base_url="http://x", complete=fake_complete(calls))
    assert again["cached"] == 4
    assert calls == []


def test_runner_falls_back_invalid_category(tmp_path):
    record, _ = record_and_pack()
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text(json.dumps(record) + "\n")

    def bad_complete(payload):
        name = payload["response_format"]["json_schema"]["name"]
        body = body_for(record_and_pack()[1], name)
        if name == "v3_role_classify":
            body.update({"major_category": "link", "sub_category": "loss",
                         "mechanism_evidence_ids": [record_and_pack()[1].signals[0].evidence_id]})
        return {"choices": [{"message": {"content": json.dumps(body)}}]}

    summary = run_roles(evidence, tmp_path / "roles", model="m", revision="r",
                        base_url="http://x", complete=bad_complete)
    assert summary["roles"]["classify"] == {"accepted": 0, "fallback": 1,
                                            "seconds": summary["roles"]["classify"]["seconds"]}
    rows = [json.loads(line) for line in (tmp_path / "roles" / "roles.jsonl").read_text().splitlines()]
    classify = next(row for row in rows if row["role"] == "classify")
    assert classify["status"] == "fallback"
    assert classify["reason"] == "mechanism_missing"


def test_runner_rejects_duplicate_evidence_ids(tmp_path):
    record, _ = record_and_pack()
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text((json.dumps(record) + "\n") * 2)
    try:
        run_roles(evidence, tmp_path / "roles", model="m", revision="r",
                  base_url="http://x", complete=fake_complete([]))
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate evidence ids must be rejected")

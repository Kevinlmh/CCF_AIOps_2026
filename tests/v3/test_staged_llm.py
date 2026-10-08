import json
from dataclasses import asdict

import pytest

from aiops_v3.contracts import load_contract
from aiops_v3.diagnosis import diagnose_rules
from aiops_v3.server_llm import run_batch
from aiops_v3.staged_llm import diagnose_staged, workflow_fingerprint
from test_evidence_context import _store, _event
from aiops_v3.diagnosis import build_evidence


def _record(tmp_path):
    pack = build_evidence(_store(tmp_path), _event(), load_contract(), include_diagnostic_context=True)
    return {**asdict(pack), "evidence_sha256": pack.sha256()}, pack


def _completion(pack, requests):
    def complete(payload):
        requests.append(payload)
        material = json.loads(payload["messages"][1]["content"])
        schema = payload["response_format"]["json_schema"]["schema"]
        name = payload["response_format"]["json_schema"]["name"]
        if name == "device_analysis":
            assert "rule_baseline" not in json.dumps(material)
            assert "score" not in json.dumps(material)
            body = {"devices": [{"node": n, "assessment": "insufficient", "evidence_ids": [],
                                 "note": "No verified causal mapping."} for n in material["nodes"]]}
        elif name == "root_ranking":
            roots = list(diagnose_rules(pack, load_contract()).roots)
            body = {"roots": roots, "evidence_ids": [material["evidence"][0]["evidence_id"]], "note": "Direct observation."}
        else:
            assert "allowed_categories" in material
            assert "rule_baseline" not in material
            body = {"category": "resource/cpu_pressure", "evidence_ids": [material["evidence"][0]["evidence_id"]], "note": "CPU observation."}
        assert "uniqueItems" not in json.dumps(schema)
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(body)}}],
                "usage": {"completion_tokens": 60}}
    return complete


def test_staged_requests_are_separate_importable_and_bounded(tmp_path):
    record, pack = _record(tmp_path)
    requests = []
    accepted, audit = diagnose_staged([record], _completion(pack, requests), "test", "r1")
    assert len(accepted) == 1
    assert accepted[0]["workflow"] == "staged"
    assert accepted[0]["prompt_sha256"] == workflow_fingerprint(pack, "test")
    assert audit[0]["status"] == "accepted"
    assert len(audit[0]["device_analyses"]) == len(pack.candidates)
    names = [p["response_format"]["json_schema"]["name"] for p in requests]
    assert names[-2:] == ["root_ranking", "fault_classification"]
    assert all(p["max_tokens"] <= 1024 for p in requests)
    assert len(accepted[0]["evidence_ids"]) <= 12


def test_intermediate_wrong_device_coverage_falls_back(tmp_path):
    record, _ = _record(tmp_path)
    calls = []
    def complete(payload):
        calls.append(payload)
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"devices": []}'}}]}
    accepted, audit = diagnose_staged([record], complete, "test", "r1")
    assert accepted == []
    assert len(calls) == 2  # one bounded retry, not an unbounded loop
    assert audit[0]["failed_stage"] == "device_analysis"
    assert "coverage" in audit[0]["detail"]


def test_staged_cache_resumes_and_rejects_single_workflow(tmp_path):
    record, pack = _record(tmp_path)
    accepted, _ = diagnose_staged([record], _completion(pack, []), "test", "r1")
    source = tmp_path / "evidence.jsonl"
    source.write_text(json.dumps(record) + "\n")
    output = tmp_path / "responses.jsonl"
    output.write_text(json.dumps(accepted[0]) + "\n")
    kwargs = dict(model="test", revision="r1", base_url="http://127.0.0.1:1/v1")
    assert run_batch(source, output, tmp_path / "audit.jsonl", workflow="staged", **kwargs)["already_accepted"] == 1
    with pytest.raises(ValueError, match="workflow|prompt"):
        run_batch(source, output, tmp_path / "audit.jsonl", **kwargs)

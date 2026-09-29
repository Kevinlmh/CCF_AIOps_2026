import json

import numpy as np

from aiops_v3.contracts import load_contract, validate_prediction
from aiops_v3.detection import detect
from aiops_v3.diagnosis import build_evidence
from aiops_v3.run import run
from aiops_v3.store import open_store


def store_with_fault(path):
    count = 14
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": ["xian-service-vm-1"], "edges": []},
        "features": {"node": ["node.cpu_usage"], "edge": [], "log": []},
        "shapes": {"node": [count, 1, 1], "edge": [count, 0, 0], "log": [count, 1, 0]},
    }
    path.mkdir()
    (path / "manifest.json").write_text(json.dumps(manifest))
    for kind, values in (
        ("node", np.array([10] * 5 + [78] * 3 + [10] * 6, np.float32)[:, None, None]),
        ("edge", np.zeros((count, 0, 0), np.float32)),
        ("log", np.zeros((count, 1, 0), np.float32)),
    ):
        np.save(path / f"{kind}_values.npy", values)
        np.save(path / f"{kind}_mask.npy", np.ones_like(values, bool))
    return path


def test_rules_run_writes_official_prediction_and_evidence(tmp_path):
    source = store_with_fault(tmp_path / "store")
    summary = run(source, tmp_path / "rules")
    prediction = json.loads((tmp_path / "rules" / "predictions.jsonl").read_text())
    validate_prediction(prediction, load_contract())
    assert summary.event_count == 1
    assert prediction["root_cause_top5"][0]["network_element_id"] == "xian-service-vm-1"
    assert json.loads((tmp_path / "rules" / "evidence.jsonl").read_text())["event_id"] == prediction["prediction_id"]
    manifest = json.loads((tmp_path / "rules" / "run_manifest.json").read_text())
    assert len(manifest["input_array_sha256"]) == 6
    assert set(manifest["output_sha256"]) == {"predictions.jsonl", "evidence.jsonl", "audit.jsonl"}
    audit = json.loads((tmp_path / "rules" / "audit.jsonl").read_text())
    assert audit["evidence_tier"] == "single_metric_direct"
    assert audit["root_has_direct_evidence"] is True


def test_invalid_server_diagnosis_falls_back_per_event(tmp_path):
    source = store_with_fault(tmp_path / "store")
    response = tmp_path / "responses.jsonl"
    response.write_text(json.dumps({
        "event_id": "v3-e000001", "root_cause_top5": ["invented-node"] * 5,
        "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"},
        "evidence_ids": [],
    }) + "\n")
    summary = run(source, tmp_path / "llm", mode="llm-jsonl", llm_responses=response)
    prediction = json.loads((tmp_path / "llm" / "predictions.jsonl").read_text())
    audit = json.loads((tmp_path / "llm" / "audit.jsonl").read_text())
    assert summary.fallback_count == 1
    assert prediction["root_cause_top5"][0]["network_element_id"] == "xian-service-vm-1"
    assert audit["fallback_reason"] == "invalid_llm_response"


def test_valid_server_diagnosis_can_reorder_only_existing_candidates(tmp_path):
    source = store_with_fault(tmp_path / "store")
    store = open_store(source)
    pack = build_evidence(store, detect(store)[0], load_contract())
    selected = [candidate.node for candidate in pack.candidates[:5]][::-1]
    response = tmp_path / "responses.jsonl"
    response.write_text(json.dumps({
        "event_id": pack.event_id, "evidence_sha256": pack.sha256(), "root_cause_top5": selected,
        "fault_category": {"major_category": "resource", "sub_category": "memory_pressure"},
        "evidence_ids": [pack.signals[0].evidence_id], "model": {"repository": "test", "revision": "r1"},
    }) + "\n")
    summary = run(source, tmp_path / "llm", mode="llm-jsonl", llm_responses=response)
    prediction = json.loads((tmp_path / "llm" / "predictions.jsonl").read_text())
    assert summary.fallback_count == 0
    assert prediction["root_cause_top5"][0]["network_element_id"] == selected[0]
    assert prediction["fault_category"]["sub_category"] == "memory_pressure"


def test_stale_server_response_with_matching_ids_falls_back(tmp_path):
    source = store_with_fault(tmp_path / "store")
    store = open_store(source)
    pack = build_evidence(store, detect(store)[0], load_contract())
    response = tmp_path / "stale.jsonl"
    response.write_text(json.dumps({
        "event_id": pack.event_id, "evidence_sha256": "0" * 64,
        "root_cause_top5": [item.node for item in pack.candidates[:5]],
        "fault_category": {"major_category": "resource", "sub_category": "memory_pressure"},
        "evidence_ids": [pack.signals[0].evidence_id],
    }) + "\n")
    summary = run(source, tmp_path / "result", "llm-jsonl", response)
    assert summary.fallback_count == 1
    assert json.loads((tmp_path / "result" / "audit.jsonl").read_text())["fallback_reason"] == "invalid_llm_response"

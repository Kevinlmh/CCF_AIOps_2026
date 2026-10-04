import json
import hashlib

import numpy as np
import pytest

from aiops_v3.merge import merge_predictions
from aiops_v3.prediction_audit import audit_two_batches
from aiops_v3.run import run


def _run_case(path, start_time):
    source = path / "store"
    source.mkdir(parents=True)
    count = 14
    manifest = {
        "start_time": start_time, "minute_count": count,
        "entities": {"nodes": ["xian-service-vm-1"], "edges": []},
        "features": {"node": ["node.cpu_usage"], "edge": [], "log": []},
        "shapes": {"node": [count, 1, 1], "edge": [count, 0, 0], "log": [count, 1, 0]},
    }
    (source / "manifest.json").write_text(json.dumps(manifest))
    for kind, values in (
        ("node", np.array([10] * 5 + [80] * 3 + [10] * 6, np.float32)[:, None, None]),
        ("edge", np.zeros((count, 0, 0), np.float32)),
        ("log", np.zeros((count, 1, 0), np.float32)),
    ):
        np.save(source / f"{kind}_values.npy", values)
        np.save(source / f"{kind}_mask.npy", np.ones_like(values, bool))
    output = path / "run"
    run(source, output)
    return output


def test_two_batch_audit_replays_merge_and_checks_evidence(tmp_path):
    first = _run_case(tmp_path / "first", "2026-07-28T12:00:00Z")
    second = _run_case(tmp_path / "second", "2026-07-29T12:00:00Z")
    merged = tmp_path / "merged.jsonl"
    merge_predictions([first / "predictions.jsonl", second / "predictions.jsonl"], merged)
    report = audit_two_batches(first, second, merged)
    assert report["verified"] is True
    assert report["prediction_count"] == 2
    assert [item["prediction_count"] for item in report["batches"]] == [1, 1]
    assert report["same_category_overlap_pairs"] == 0


def test_two_batch_audit_rejects_merged_file_that_is_not_exact_replay(tmp_path):
    first = _run_case(tmp_path / "first", "2026-07-28T12:00:00Z")
    second = _run_case(tmp_path / "second", "2026-07-29T12:00:00Z")
    merged = tmp_path / "merged.jsonl"
    merge_predictions([first / "predictions.jsonl", second / "predictions.jsonl"], merged)
    rows = [json.loads(line) for line in merged.read_text().splitlines()]
    rows[0]["fault_category"]["sub_category"] = "memory_pressure"
    merged.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    with pytest.raises(ValueError, match="merged file differs"):
        audit_two_batches(first, second, merged)


def test_two_batch_audit_rejects_tampered_evidence(tmp_path):
    first = _run_case(tmp_path / "first", "2026-07-28T12:00:00Z")
    second = _run_case(tmp_path / "second", "2026-07-29T12:00:00Z")
    merged = tmp_path / "merged.jsonl"
    merge_predictions([first / "predictions.jsonl", second / "predictions.jsonl"], merged)
    evidence = first / "evidence.jsonl"
    row = json.loads(evidence.read_text())
    row["signals"][0]["value"] = 123
    evidence.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="evidence.jsonl hash"):
        audit_two_batches(first, second, merged)


def test_two_batch_audit_rejects_prediction_time_drift_from_evidence(tmp_path):
    first = _run_case(tmp_path / "first", "2026-07-28T12:00:00Z")
    second = _run_case(tmp_path / "second", "2026-07-29T12:00:00Z")
    prediction_path = first / "predictions.jsonl"
    row = json.loads(prediction_path.read_text())
    row["start_time"] = "2026-07-28T12:06:00Z"
    prediction_path.write_text(json.dumps(row, sort_keys=True) + "\n")
    manifest_path = first / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["output_sha256"]["predictions.jsonl"] = hashlib.sha256(prediction_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    merged = tmp_path / "merged.jsonl"
    merge_predictions([prediction_path, second / "predictions.jsonl"], merged)
    with pytest.raises(ValueError, match="prediction and evidence times differ"):
        audit_two_batches(first, second, merged)

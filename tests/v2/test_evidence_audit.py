from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys

import pytest

from aiops_challenge_2026.config import load_public_config
from aiops_v2.data.feature_store import build_feature_store
from aiops_v2.evidence_audit import audit_run
from aiops_v2.run import main
from baseline.bian.preprocessing.observations import NumericObservation


START = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)


def _observation(minute, node, metric, value):
    return NumericObservation(
        START + timedelta(minutes=minute), "node", node, (), metric,
        float(value), (), "high",
    )


def _write_run(tmp_path, category, roots):
    prediction = {
        "prediction_id": "pred_000001",
        "start_time": "2026-08-19T04:30:00.000Z",
        "end_time": "2026-08-19T04:36:00.000Z",
        "root_cause_top5": [
            {"rank": index + 1, "network_element_id": node}
            for index, node in enumerate(roots)
        ],
        "fault_category": {"major_category": "resource", "sub_category": category},
    }
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(json.dumps(prediction) + "\n", encoding="utf-8")
    inference = tmp_path / "inference.json"
    inference.write_text(json.dumps({"events": [{
        "prediction_id": "pred_000001",
        "event": {"start_index": 30, "end_index": 35, "peak_index": 32},
    }]}), encoding="utf-8")
    return predictions, inference


def test_audit_exposes_disk_label_without_root_disk_anchor_and_preserves_prediction(tmp_path):
    root = "beida-service-vm-1"
    other = "wuhan-service-vm-2"
    observations = []
    for minute in range(80):
        observations.extend((
            _observation(minute, root, "node.cpu_usage", 45 if 30 <= minute < 36 else 2),
            _observation(minute, root, "node.disk_io_util", 3),
            _observation(minute, other, "node.disk_io_util", 90 if 30 <= minute < 36 else 3),
        ))
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    roots = (root, other, "beida-service-vm-2", "beida-br-1", "wuhan-br-1")
    predictions, inference = _write_run(tmp_path, "disk_io_pressure", roots)
    original = predictions.read_bytes()

    report = audit_run(store, predictions, inference)

    assert predictions.read_bytes() == original
    assert report["summary"]["event_count"] == 1
    assert report["summary"]["unsupported_disk_root_count"] == 1
    trace = report["events"][0]
    assert trace["root_node"] == root
    assert trace["classification_anchor"]["status"] == "unsupported"
    assert trace["classification_anchor"]["raw_peak"] == 3.0
    assert trace["peak_driver"]["kind"] == "node"
    assert trace["peak_driver"]["entity"] == root


def test_audit_marks_equal_node_drivers_ambiguous(tmp_path):
    roots = ("beida-service-vm-1", "wuhan-service-vm-2", "beida-br-1", "wuhan-br-1", "beida-fw")
    observations = [
        _observation(minute, node, "node.cpu_usage", 45 if 30 <= minute < 36 else 2)
        for minute in range(80)
        for node in roots[:2]
    ]
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    predictions, inference = _write_run(tmp_path, "cpu_pressure", roots)

    trace = audit_run(store, predictions, inference)["events"][0]

    assert trace["peak_driver"]["kind"] == "ambiguous"
    assert trace["peak_driver"]["entity"] is None
    assert trace["classification_anchor"]["status"] == "supported"


def test_audit_cli_writes_separate_report_without_changing_predictions(tmp_path, monkeypatch):
    root = "beida-service-vm-1"
    observations = [
        _observation(minute, root, "node.cpu_usage", 45 if 30 <= minute < 36 else 2)
        for minute in range(80)
    ]
    store_path = tmp_path / "store"
    build_feature_store(observations, load_public_config("network_elements"), store_path)
    predictions, inference = _write_run(
        tmp_path, "cpu_pressure",
        (root, "beida-br-1", "beida-br-2", "beida-cr-1", "beida-cr-2"),
    )
    original = predictions.read_bytes()
    report_path = tmp_path / "audit.json"
    monkeypatch.setattr(sys, "argv", [
        "aiops_v2.run", "audit", "--store", str(store_path),
        "--predictions", str(predictions), "--inference-log", str(inference),
        "--output", str(report_path),
    ])

    assert main() == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["summary"]["event_count"] == 1
    assert predictions.read_bytes() == original


def test_audit_rejects_duplicate_inference_ids_even_if_counts_match(tmp_path):
    root = "beida-service-vm-1"
    store = build_feature_store(
        [_observation(minute, root, "node.cpu_usage", 2) for minute in range(80)],
        load_public_config("network_elements"), tmp_path / "store",
    )
    predictions, inference = _write_run(
        tmp_path, "cpu_pressure",
        (root, "beida-br-1", "beida-br-2", "beida-cr-1", "beida-cr-2"),
    )
    second = json.loads(predictions.read_text(encoding="utf-8"))
    second["prediction_id"] = "pred_000002"
    predictions.write_text(predictions.read_text(encoding="utf-8") + json.dumps(second) + "\n", encoding="utf-8")
    logged = json.loads(inference.read_text(encoding="utf-8"))
    logged["events"].append(dict(logged["events"][0]))
    inference.write_text(json.dumps(logged), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate inference"):
        audit_run(store, predictions, inference)


def test_audit_distinguishes_raw_netflow_peak_from_effective_ranking_contribution(tmp_path):
    root = "beida-br-1"
    observations = []
    for minute in range(80):
        timestamp = START + timedelta(minutes=minute)
        observations.extend((
            _observation(minute, root, "node.cpu_usage", 45 if 30 <= minute < 36 else 2),
            NumericObservation(timestamp, "netflow", root, (), "netflow.flow_records", 40,
                               (("protocol", "6"), ("interface_id", "eth0")), "high"),
            NumericObservation(timestamp, "netflow", root, (), "netflow.protocol_byte_share",
                               0.9 if 25 <= minute < 36 else 0.2,
                               (("protocol", "6"), ("interface_id", "eth0")), "high"),
        ))
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store",
    )
    predictions, inference = _write_run(
        tmp_path, "cpu_pressure",
        (root, "beida-br-2", "beida-cr-1", "beida-cr-2", "beida-fw"),
    )
    logged = json.loads(inference.read_text(encoding="utf-8"))
    logged["events"][0]["root_scores"] = [{
        "node_id": root,
        "components": {"netflow": 0.0, "effective_netflow": 0.0},
    }]
    inference.write_text(json.dumps(logged), encoding="utf-8")

    trace = audit_run(store, predictions, inference)["events"][0]

    assert trace["root_netflow_raw_support_peak"] > 0
    assert trace["root_netflow_effective_contribution"] == 0.0

import hashlib
import json

import numpy as np
import pytest

from aiops_v3.calibration import load_calibration, store_fingerprint
from aiops_v3.run import run
from data_side.threshold_audit import audit_feature_store


def _store(path, *, cpu_peak=35, source_profile="stage2"):
    path.mkdir()
    count = 16
    manifest = {
        "start_time": "2026-09-17T04:00:00Z", "minute_count": count,
        "source_profile": source_profile,
        "entities": {"nodes": ["xian-service-vm-1"], "edges": []},
        "features": {"node": ["node.cpu_usage"], "edge": [], "log": []},
        "shapes": {"node": [count, 1, 1], "edge": [count, 0, 0], "log": [count, 1, 0]},
    }
    (path / "manifest.json").write_text(json.dumps(manifest))
    node = np.array([10] * 5 + [cpu_peak] * 3 + [10] * 8, np.float32)[:, None, None]
    for kind, values in (("node", node), ("edge", np.zeros((count, 0, 0), np.float32)),
                         ("log", np.zeros((count, 1, 0), np.float32))):
        np.save(path / f"{kind}_values.npy", values)
        np.save(path / f"{kind}_mask.npy", np.ones_like(values, bool))
    return path


def _validated_profile(path, store, *, overrides):
    profile = {
        "format_version": 1,
        "input_manifest_sha256": hashlib.sha256((store / "manifest.json").read_bytes()).hexdigest(),
        "input_store_sha256": store_fingerprint(store),
        "source_profile": "stage2",
        "status": "validated",
        "validation": {"normal_window_count": 1, "fault_window_count": 1},
        "rule_overrides": overrides,
    }
    path.write_text(json.dumps(profile))
    return path


def test_audit_is_bound_to_input_store_and_reports_reference_shift(tmp_path):
    reference = _store(tmp_path / "first", cpu_peak=60, source_profile="stage1")
    current = _store(tmp_path / "second")
    report = audit_feature_store(current, reference_store=reference)
    assert report["status"] == "audit_only"
    assert report["rule_overrides"] == {}
    assert report["source_profile"] == "stage2"
    assert report["input_manifest_sha256"] == hashlib.sha256(
        (current / "manifest.json").read_bytes()).hexdigest()
    assert report["input_store_sha256"] == store_fingerprint(current)
    assert report["reference_store_sha256"] == store_fingerprint(reference)
    cpu = next(item for item in report["rules"] if item["feature"] == "node.cpu_usage")
    assert cpu["observed_cells"] == 16
    assert cpu["value_p99"] < cpu["reference_value_p99"]
    assert cpu["active_cells"] == 3
    assert cpu["active_entities_top5"] == [{"node_id": "xian-service-vm-1", "active_cells": 3}]
    assert cpu["active_windows_top10"][0]["node_id"] == "xian-service-vm-1"
    assert cpu["active_windows_top10"][0]["start_time"] == "2026-09-17T04:05:00Z"
    assert cpu["active_windows_top10"][0]["end_time"] == "2026-09-17T04:08:00Z"
    assert cpu["active_per_100k_observed"] == pytest.approx(3 / 16 * 100000)
    assert cpu["reference_active_per_100k_observed"] == pytest.approx(3 / 16 * 100000)
    assert "value_p99_shift" in cpu["review_reasons"]
    assert report["covered_node_features"] == ["node.cpu_usage"]
    assert report["uncovered_node_features"] == []


def test_matching_validated_override_changes_detection_and_is_recorded(tmp_path):
    store = _store(tmp_path / "store")
    profile = _validated_profile(tmp_path / "calibration.json", store,
                                 overrides={"node.cpu_usage": {"score_threshold": 7.0}})
    assert run(store, tmp_path / "default").event_count == 1
    assert run(store, tmp_path / "calibrated", calibration_profile=profile).event_count == 0
    manifest = json.loads((tmp_path / "calibrated" / "run_manifest.json").read_text())
    assert manifest["calibration_profile_sha256"] == hashlib.sha256(profile.read_bytes()).hexdigest()
    assert manifest["calibration_status"] == "validated"
    assert manifest["detector_settings"]["rule_overrides"] == {
        "node.cpu_usage": {"score_threshold": 7.0}}


def test_audit_only_profile_runs_without_changing_thresholds(tmp_path):
    store = _store(tmp_path / "store")
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(audit_feature_store(store)))
    assert run(store, tmp_path / "audited", calibration_profile=path).event_count == 1
    assert json.loads((tmp_path / "audited" / "run_manifest.json").read_text())["calibration_status"] == "audit_only"


def test_rejects_profile_for_other_store_or_unreviewed_override(tmp_path):
    store = _store(tmp_path / "store")
    different = _store(tmp_path / "different", cpu_peak=60)
    profile = _validated_profile(tmp_path / "calibration.json", different,
                                 overrides={"node.cpu_usage": {"score_threshold": 7.0}})
    with pytest.raises(ValueError, match="store content"):
        load_calibration(profile, store)
    unreviewed = audit_feature_store(store)
    unreviewed["rule_overrides"] = {"node.cpu_usage": {"score_threshold": 7.0}}
    profile.write_text(json.dumps(unreviewed))
    with pytest.raises(ValueError, match="audit_only"):
        load_calibration(profile, store)


def test_rejects_invalid_rule_or_nonfinite_threshold(tmp_path):
    store = _store(tmp_path / "store")
    profile = _validated_profile(tmp_path / "calibration.json", store,
                                 overrides={"unknown.metric": {"score_threshold": 7.0}})
    with pytest.raises(ValueError, match="unknown rule"):
        load_calibration(profile, store)
    _validated_profile(profile, store, overrides={"node.cpu_usage": {"score_threshold": float("nan")}})
    with pytest.raises(ValueError, match="finite"):
        load_calibration(profile, store)


def test_rejects_override_for_rule_absent_from_store(tmp_path):
    store = _store(tmp_path / "store")
    profile = _validated_profile(tmp_path / "calibration.json", store,
                                 overrides={"node.memory_available_ratio": {"absolute": .8}})
    with pytest.raises(ValueError, match="absent from input store"):
        load_calibration(profile, store)


def test_profile_fingerprint_tracks_dimension_sidecar(tmp_path):
    store = _store(tmp_path / "store")
    (store / "dimension_series.json").write_text("[]")
    profile = _validated_profile(tmp_path / "calibration.json", store,
                                 overrides={"node.cpu_usage": {"score_threshold": 7.0}})
    (store / "dimension_series.json").write_text("[1]")
    with pytest.raises(ValueError, match="store content"):
        load_calibration(profile, store)

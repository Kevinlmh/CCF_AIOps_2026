"""Validate a feature-store-bound threshold profile before inference."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .detection import _NODE_RULES


def store_fingerprint(input_store: Path) -> str:
    """Hash the manifest, arrays and optional sidecars used by inference."""
    root = Path(input_store)
    digest = hashlib.sha256()
    names = ["manifest.json", *(f"{kind}_{item}.npy"
                               for kind in ("node", "edge", "log")
                               for item in ("values", "mask"))]
    names.extend(name for name in ("dimension_series.json", "dimension_cells.npy", "text_evidence.jsonl")
                 if (root / name).is_file())
    for name in names:
        digest.update(name.encode("utf-8") + b"\0")
        with (root / name).open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def load_calibration(path: Path, input_store: Path) -> dict[str, dict[str, float]]:
    """Return reviewed rule overrides; audit-only profiles retain current rules."""
    profile = json.loads(Path(path).read_text(encoding="utf-8"))
    if profile.get("format_version") != 1:
        raise ValueError("unsupported calibration profile format")
    manifest_path = Path(input_store) / "manifest.json"
    actual_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if profile.get("input_manifest_sha256") != actual_hash:
        raise ValueError("calibration profile does not match input manifest")
    if profile.get("input_store_sha256") != store_fingerprint(input_store):
        raise ValueError("calibration profile does not match input store content")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_profile = manifest.get("source_profile", "unknown")
    if profile.get("source_profile") != source_profile:
        raise ValueError("calibration profile source_profile does not match input manifest")
    status = profile.get("status")
    if status not in {"audit_only", "validated"}:
        raise ValueError("unknown calibration status")
    overrides = profile.get("rule_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("calibration rule_overrides must be an object")
    if status == "audit_only" and overrides:
        raise ValueError("audit_only profile cannot override detector thresholds")
    validation = profile.get("validation", {})
    if overrides and (not isinstance(validation, dict)
                      or validation.get("normal_window_count", 0) < 1
                      or validation.get("fault_window_count", 0) < 1):
        raise ValueError("threshold overrides require reviewed normal and fault windows")
    known = {rule.feature for rule in _NODE_RULES}
    checked: dict[str, dict[str, float]] = {}
    for feature, values in overrides.items():
        if feature not in known:
            raise ValueError(f"unknown rule in calibration profile: {feature}")
        if feature not in manifest["features"]["node"]:
            raise ValueError(f"calibration rule absent from input store: {feature}")
        if not isinstance(values, dict) or not values:
            raise ValueError(f"calibration override for {feature} must be a nonempty object")
        checked[feature] = {}
        for field, value in values.items():
            if field not in {"score_threshold", "floor", "absolute"}:
                raise ValueError(f"unknown calibration field: {field}")
            if type(value) not in {int, float} or not math.isfinite(value):
                raise ValueError(f"calibration {field} must be finite")
            if field != "absolute" and value <= 0:
                raise ValueError(f"calibration {field} must be positive")
            checked[feature][field] = float(value)
    return checked

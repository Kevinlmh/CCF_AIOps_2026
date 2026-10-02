"""Create a reproducible, label-aware audit of detector thresholds for a feature store."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from aiops_v3.calibration import store_fingerprint
from aiops_v3.detection import DetectorSettings, _NODE_RULES, _groups, _scores, _supported
from aiops_v3.store import FeatureStore, open_store


def _rule_stats(store: FeatureStore, feature: str, direction: int, floor: float,
                absolute: float, immediate: bool) -> dict:
    column = store.feature_index("node", feature)
    if column is None:
        return {"available": False, "observed_cells": 0, "active_cells": 0}
    values = np.asarray(store.node_values[:, :, column], dtype=np.float32)
    mask = np.asarray(store.node_mask[:, :, column], dtype=bool) & np.isfinite(values)
    observed = values[mask]
    if not len(observed):
        return {"available": True, "observed_cells": 0, "active_cells": 0,
                "observed_fraction": 0.0}
    from aiops_v3.detection import _Rule
    rule = _Rule(feature, "audit", direction, floor, absolute, immediate)
    scores, _ = _scores(values, mask, rule)
    active = _supported(scores, DetectorSettings().score_threshold, immediate)
    entity_counts = np.count_nonzero(active, axis=0)
    top_entities = sorted(
        ({"node_id": node, "active_cells": int(entity_counts[index])}
         for index, node in enumerate(store.nodes) if entity_counts[index]),
        key=lambda item: (-item["active_cells"], item["node_id"]),
    )[:5]
    windows = []
    for index, node in enumerate(store.nodes):
        if not entity_counts[index]:
            continue
        for start, end in _groups(active[:, index], DetectorSettings()):
            windows.append({
                "node_id": node,
                "start_time": store.time_at(start).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "end_time": store.time_at(end).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "active_cells": int(np.count_nonzero(active[start:end, index])),
                "peak_score": float(np.max(scores[start:end, index])),
            })
    windows.sort(key=lambda item: (-item["peak_score"], -item["active_cells"],
                                   item["start_time"], item["node_id"]))
    return {
        "available": True,
        "observed_cells": int(mask.sum()),
        "observed_fraction": float(mask.mean()),
        "value_p50": float(np.quantile(observed, .5)),
        "value_p95": float(np.quantile(observed, .95)),
        "value_p99": float(np.quantile(observed, .99)),
        "active_cells": int(np.count_nonzero(active)),
        "active_per_100k_observed": float(np.count_nonzero(active) / len(observed) * 100000),
        "active_entities_top5": top_entities,
        "active_windows_top10": windows[:10],
    }


def _review_reasons(current: dict, previous: dict) -> list[str]:
    """Flag distribution changes for review without changing thresholds."""
    reasons = []
    current_p99, previous_p99 = current.get("value_p99"), previous.get("value_p99")
    if current_p99 is not None and previous_p99 is not None:
        if ((previous_p99 == 0 and current_p99 != 0)
                or (previous_p99 != 0 and abs(current_p99 / previous_p99 - 1) >= .25)):
            reasons.append("value_p99_shift")
    current_rate = current.get("active_per_100k_observed")
    previous_rate = previous.get("active_per_100k_observed")
    if current_rate is not None and previous_rate is not None:
        if ((current["active_cells"] >= 10 and previous["active_cells"] == 0)
                or (previous["active_cells"] >= 10 and current["active_cells"] == 0)
                or (current["active_cells"] >= 10 and previous["active_cells"] >= 10
                    and (current_rate / previous_rate >= 2 or current_rate / previous_rate <= .5))):
            reasons.append("active_rate_shift")
    return reasons


def audit_feature_store(input_store: Path, *, reference_store: Path | None = None) -> dict:
    """Profile current rules without treating unlabelled telemetry as healthy truth."""
    input_store = Path(input_store)
    store = open_store(input_store)
    reference = open_store(Path(reference_store)) if reference_store is not None else None
    rows = []
    for rule in _NODE_RULES:
        current = _rule_stats(store, rule.feature, rule.direction, rule.floor,
                              rule.absolute, rule.immediate)
        row = {"feature": rule.feature, "score_threshold": DetectorSettings().score_threshold,
               "floor": rule.floor, "absolute": rule.absolute, **current}
        if reference is not None:
            previous = _rule_stats(reference, rule.feature, rule.direction, rule.floor,
                                   rule.absolute, rule.immediate)
            row["reference_observed_fraction"] = previous.get("observed_fraction")
            row["reference_value_p99"] = previous.get("value_p99")
            row["reference_active_cells"] = previous["active_cells"]
            row["reference_active_per_100k_observed"] = previous.get("active_per_100k_observed")
            row["review_reasons"] = _review_reasons(current, previous)
        rows.append(row)
    covered = {rule.feature for rule in _NODE_RULES if store.feature_index("node", rule.feature) is not None}
    return {
        "format_version": 1,
        "input_store": str(input_store),
        "input_manifest_sha256": hashlib.sha256((input_store / "manifest.json").read_bytes()).hexdigest(),
        "input_store_sha256": store_fingerprint(input_store),
        "source_profile": store.manifest.get("source_profile", "unknown"),
        "reference_store": str(reference_store) if reference_store is not None else None,
        "reference_manifest_sha256": (
            hashlib.sha256((Path(reference_store) / "manifest.json").read_bytes()).hexdigest()
            if reference_store is not None else None),
        "reference_store_sha256": (store_fingerprint(Path(reference_store))
                                   if reference_store is not None else None),
        "status": "audit_only",
        "validation": {"normal_window_count": 0, "fault_window_count": 0},
        "rule_overrides": {},
        "covered_node_features": sorted(covered),
        "uncovered_node_features": sorted(set(store.manifest["features"]["node"]) - covered),
        "untriggered_edge_features": sorted(name for name in store.manifest["features"]["edge"]
                                           if not (name.startswith("traffic.") and name.endswith(
                                               (".error_ratio", ".loss_rate", ".latency_p95_seconds")))),
        "rules": rows,
        "caveat": "Unlabelled distributions and active counts are coverage diagnostics, not accuracy or threshold optima.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-store", type=Path, required=True)
    parser.add_argument("--reference-store", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    report = audit_feature_store(args.input_store, reference_store=args.reference_store)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8")
    print(json.dumps({"output": str(args.output), "source_profile": report["source_profile"],
                      "rules": len(report["rules"]), "status": report["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

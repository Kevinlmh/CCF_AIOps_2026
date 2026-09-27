"""Read-only provenance audit for direct-mode predictions and feature stores."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any

import numpy as np

from aiops_challenge_2026.schema import validate_prediction
from aiops_v2.detection.direct_evidence import score_direct_evidence
from aiops_v2.detection.source_support import score_frr_support, score_netflow_support


_ANCHORS = {
    ("resource", "cpu_pressure"): ("cpu_pressure", "node.cpu_usage", 3.0, "high"),
    ("firewall", "cpu_pressure"): ("cpu_pressure", "node.cpu_usage", 3.0, "high"),
    ("resource", "memory_pressure"): ("memory_pressure", "node.memory_available_ratio", 4.0, "low"),
    ("resource", "disk_io_pressure"): ("disk_io_pressure", "node.disk_io_util", 5.0, "high"),
    ("resource", "disk_space_low"): ("disk_space_pressure", "node.filesystem_used_ratio", 4.0, "high"),
}


def _peak_driver(store, direct, peak: int, city_id: str | None = None) -> dict[str, Any]:
    node_indexes = [
        index for index, node in enumerate(store.entities.nodes)
        if city_id is None or node.startswith(f"{city_id}-")
    ]
    node_scores = direct.node_probability[peak, node_indexes]
    top_node = float(np.max(node_scores, initial=0.0))
    edge_indexes = [
        index for index, edge in enumerate(store.entities.edges)
        if edge.relation == "traffic"
        and (city_id is None or edge.target.startswith(f"service-group:{city_id}:"))
    ]
    edge_probabilities: dict[int, float] = {}
    if direct.edge_symptom_scores is not None:
        for edge_index in edge_indexes:
            strength = float(direct.edge_symptom_scores[peak, edge_index])
            if strength <= 0:
                continue
            raw = 1.0 / (1.0 + np.exp(-np.clip((strength - 3.0) / 0.9, -20.0, 20.0)))
            adjacent = (
                peak > 0 and direct.edge_symptom_scores[peak - 1, edge_index] >= 2.5
            ) or (
                peak + 1 < direct.edge_symptom_scores.shape[0]
                and direct.edge_symptom_scores[peak + 1, edge_index] >= 2.5
            )
            edge_probabilities[edge_index] = (0.9 if adjacent else 0.4) * raw
    service = max(edge_probabilities.values(), default=0.0)
    if max(top_node, service) <= 0:
        return {"kind": "undetermined", "entity": None, "score": 0.0}
    if abs(top_node - service) <= 1e-6 and service > 0:
        return {"kind": "ambiguous", "entity": None, "score": max(top_node, service)}
    if service > top_node:
        maximum = max(edge_probabilities.values())
        indexes = [index for index, score in edge_probabilities.items() if np.isclose(score, maximum, atol=1e-6)]
        if len(indexes) != 1:
            return {"kind": "ambiguous", "entity": None, "score": service}
        edge = store.entities.edges[int(indexes[0])]
        return {"kind": "service", "entity": edge.to_dict(), "score": service}
    indexes = np.flatnonzero(np.isclose(node_scores, top_node, atol=1e-6))
    if indexes.size != 1:
        return {"kind": "ambiguous", "entity": None, "score": top_node}
    return {
        "kind": "node",
        "entity": store.entities.nodes[node_indexes[int(indexes[0])]],
        "score": top_node,
    }


def _anchor(store, direct, root_index: int, start: int, stop: int, category: dict[str, str]) -> dict[str, Any]:
    definition = _ANCHORS.get((category["major_category"], category["sub_category"]))
    if definition is None:
        return {"status": "not_evaluated", "metric": None, "direct_strength": None, "raw_peak": None}
    family, metric, minimum_strength, direction = definition
    names = store.features.names("node")
    if metric not in names:
        return {"status": "undetermined", "metric": metric, "direct_strength": 0.0, "raw_peak": None}
    feature = names.index(metric)
    observed = np.asarray(store.node_mask[start:stop, root_index, feature], dtype=bool)
    values = np.asarray(store.node_values[start:stop, root_index, feature], dtype=np.float32)
    if not observed.any():
        return {"status": "undetermined", "metric": metric, "direct_strength": 0.0, "raw_peak": None}
    raw_peak = float(np.min(values[observed]) if direction == "low" else np.max(values[observed]))
    strength = float(np.max(direct.category_scores[start:stop, root_index, direct.category_names.index(family)]))
    full_values = np.asarray(store.node_values[:, root_index, feature], dtype=np.float32)
    full_mask = np.asarray(store.node_mask[:, root_index, feature], dtype=bool)
    reference = float(np.median(full_values[full_mask])) if full_mask.any() else None
    return {
        "status": "supported" if strength >= minimum_strength else "unsupported",
        "metric": metric,
        "direct_strength": strength,
        "raw_peak": raw_peak,
        "reference_median": reference,
    }


def audit_run(store, predictions_path: Path, inference_path: Path) -> dict[str, Any]:
    """Recompute source evidence from a feature store; never rewrite predictions."""
    predictions: dict[str, dict[str, Any]] = {}
    with Path(predictions_path).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            validate_prediction(item)
            identity = item["prediction_id"]
            if identity in predictions:
                raise ValueError(f"duplicate prediction ID: {identity}")
            predictions[identity] = item
    inference = json.loads(Path(inference_path).read_text(encoding="utf-8"))
    events = inference.get("events")
    if not isinstance(events, list):
        raise ValueError("inference log has no events list")
    if len(predictions) != len(events):
        raise ValueError("prediction and inference event counts differ")
    inference_ids = [item["prediction_id"] for item in events]
    if len(set(inference_ids)) != len(inference_ids):
        raise ValueError("duplicate inference prediction ID")
    if set(inference_ids) != set(predictions):
        raise ValueError("prediction and inference IDs differ")
    direct = score_direct_evidence(store)
    log_support = score_frr_support(store)
    netflow_support = score_netflow_support(store)
    netflow_by_source: dict[str, list[int]] = {}
    for index, edge in enumerate(store.entities.edges):
        if edge.relation == "netflow":
            netflow_by_source.setdefault(edge.source, []).append(index)
    traces: list[dict[str, Any]] = []
    driver_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    anchor_counts: Counter[str] = Counter()
    unsupported_disk = 0
    trigger_root_city_conflicts = 0
    cross_city_roots = 0
    scoped_root_city_violations = 0
    for item in events:
        identity = item["prediction_id"]
        if identity not in predictions:
            raise ValueError(f"inference event has no prediction: {identity}")
        prediction = predictions[identity]
        event = item["event"]
        start, stop, peak = int(event["start_index"]), int(event["end_index"]) + 1, int(event["peak_index"])
        if not 0 <= start <= peak < stop <= direct.global_probability.shape[0]:
            raise ValueError(f"invalid event indexes: {identity}")
        root = prediction["root_cause_top5"][0]["network_element_id"]
        root_index = store.entities.node_index(root)
        city_id = event.get("city_id")
        root_city_scoped = bool(event.get("root_city_scoped", False))
        driver = _peak_driver(store, direct, peak, city_id)
        anchor = _anchor(store, direct, root_index, start, stop, prediction["fault_category"])
        support_indexes = netflow_by_source.get(root, ())
        netflow_peak = (
            float(np.max(netflow_support[start:stop, support_indexes]))
            if support_indexes else 0.0
        )
        root_scores = item.get("root_scores") or []
        top_score = root_scores[0] if root_scores else None
        if top_score is not None and top_score.get("node_id") != root:
            raise ValueError(f"inference Top1 disagrees with prediction: {identity}")
        effective_netflow = (
            top_score.get("components", {}).get("effective_netflow")
            if top_score is not None else None
        )
        label = prediction["fault_category"]
        label_name = f"{label['major_category']}.{label['sub_category']}"
        driver_counts[driver["kind"]] += 1
        category_counts[label_name] += 1
        anchor_counts[anchor["status"]] += 1
        unsupported_disk += label_name == "resource.disk_io_pressure" and anchor["status"] == "unsupported"
        driver_city = (
            str(driver["entity"]).split("-", 1)[0]
            if driver["kind"] == "node"
            else str(driver["entity"].get("target", "")).split(":")[1]
            if driver["kind"] == "service" and driver["entity"]
            else None
        )
        if driver_city and driver_city != root.split("-", 1)[0]:
            trigger_root_city_conflicts += 1
        if city_id and root.split("-", 1)[0] != city_id:
            cross_city_roots += 1
            if root_city_scoped:
                scoped_root_city_violations += 1
        traces.append({
            "prediction_id": identity,
            "start_index": start,
            "end_index": stop - 1,
            "peak_index": peak,
            "city_id": city_id,
            "root_city_scoped": root_city_scoped,
            "peak_driver": driver,
            "root_node": root,
            "root_direct_strength": float(np.max(direct.category_scores[start:stop, root_index])),
            "root_frr_support_peak": float(np.max(log_support[start:stop, root_index])),
            "root_netflow_raw_support_peak": netflow_peak,
            "root_netflow_effective_contribution": (
                float(effective_netflow) if effective_netflow is not None else None
            ),
            "classification": label,
            "classification_anchor": anchor,
        })
    return {
        "summary": {
            "event_count": len(traces),
            "driver_counts": dict(driver_counts),
            "category_counts": dict(category_counts),
            "anchor_status_counts": dict(anchor_counts),
            "unsupported_disk_root_count": int(unsupported_disk),
            "trigger_root_city_conflict_count": trigger_root_city_conflicts,
            "cross_city_root_count": cross_city_roots,
            "root_city_scope_violation_count": scoped_root_city_violations,
        },
        "events": traces,
    }

"""Compact, read-only attribution of high neural detector scores."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import numpy as np

from aiops_v2.contracts import EdgeKey
from aiops_v2.training.inference import TimelineScores


FAMILIES = ("node", "edge", "log")
COMPONENT_WEIGHTS = {"level": 0.45, "reconstruction": 0.25, "forecast": 0.30}


def build_score_audit(
    probabilities: np.ndarray,
    timeline: TimelineScores,
    standardized: np.ndarray,
    origin: datetime,
    *,
    nodes: Sequence[str],
    edges: Sequence[EdgeKey],
    threshold: float = 0.8,
    edge_trigger_eligibility: np.ndarray | None = None,
) -> dict[str, object]:
    """Report observed score families and error terms, without fault labels."""
    probabilities = np.asarray(probabilities, dtype=np.float64)
    standardized = np.asarray(standardized, dtype=np.float64)
    minute_count = timeline.family.shape[0]
    if probabilities.shape != (minute_count,) or standardized.shape != (minute_count, 3):
        raise ValueError("probabilities and standardized scores need matching minute counts")
    if not 0 <= threshold <= 1 or not np.isfinite(probabilities).all():
        raise ValueError("threshold and probabilities must be finite and within [0, 1]")
    if np.any(probabilities < 0) or np.any(probabilities > 1):
        raise ValueError("probabilities must be within [0, 1]")
    if not np.isfinite(standardized[timeline.family_observed]).all():
        raise ValueError("observed standardized scores must be finite")
    if (timeline.node.shape != (minute_count, len(nodes))
            or timeline.edge.shape != (minute_count, len(edges))
            or timeline.log.shape != (minute_count, len(nodes))):
        raise ValueError("timeline entity scores do not match node and edge registries")
    edge_eligible = None
    if edge_trigger_eligibility is not None:
        edge_eligible = np.asarray(edge_trigger_eligibility, dtype=bool)
        if edge_eligible.shape != timeline.edge.shape:
            raise ValueError("edge trigger eligibility must match [T, E] scores")

    high_minutes: list[dict[str, object]] = []
    family_counts: Counter[str] = Counter()
    component_counts: Counter[str] = Counter()
    for minute in np.flatnonzero(probabilities >= threshold):
        observed = timeline.family_observed[minute]
        family_z = {
            name: float(standardized[minute, index]) if observed[index] else None
            for index, name in enumerate(FAMILIES)
        }
        family = (
            FAMILIES[int(np.argmax(np.where(observed, standardized[minute], -np.inf)))]
            if np.any(observed) else None
        )
        top_entity = None
        top_entity_score = None
        weighted_components = None
        dominant_component = None
        if family is not None:
            entities = edges if family == "edge" else nodes
            scores = getattr(timeline, family)[minute]
            if len(entities):
                if family == "edge" and edge_eligible is not None:
                    eligible_indexes = np.flatnonzero(edge_eligible[minute])
                    index = (
                        int(eligible_indexes[np.argmax(scores[eligible_indexes])])
                        if eligible_indexes.size
                        else int(np.argmax(scores))
                    )
                else:
                    index = int(np.argmax(scores))
                entity = entities[index]
                top_entity = entity.to_dict() if isinstance(entity, EdgeKey) else entity
                top_entity_score = float(scores[index])
                if all(f"{family}_{name}" in timeline.components for name in COMPONENT_WEIGHTS):
                    weighted_components = {
                        name: float(
                            COMPONENT_WEIGHTS[name] * timeline.components[f"{family}_{name}"][minute, index]
                        )
                        for name in COMPONENT_WEIGHTS
                    }
                    dominant_component = max(weighted_components, key=weighted_components.get)
            family_counts[family] += 1
            if dominant_component is not None:
                component_counts[f"{family}.{dominant_component}"] += 1
        high_minutes.append({
            "minute_index": int(minute),
            "timestamp": (origin.astimezone(timezone.utc) + timedelta(minutes=int(minute)))
                .isoformat(timespec="seconds").replace("+00:00", "Z"),
            "probability": float(probabilities[minute]),
            "family_z_scores": family_z,
            "dominant_family": family,
            "top_entity": top_entity,
            "top_entity_score": top_entity_score,
            "weighted_components": weighted_components,
            "dominant_component": dominant_component,
        })
    return {
        "summary": {
            "threshold": float(threshold),
            "total_minute_count": int(minute_count),
            "high_minute_count": len(high_minutes),
            "dominant_family_counts": dict(sorted(family_counts.items())),
            "dominant_component_counts": dict(sorted(component_counts.items())),
        },
        "high_minutes": high_minutes,
    }

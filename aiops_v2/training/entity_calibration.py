"""Robust per-entity calibration for heterogeneous telemetry scores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True, slots=True)
class EntityScoreCalibrator:
    """Normalize anomaly scores independently for each entity.

    Scores are log-transformed before robust location/scale estimation. Entities
    with fewer than ``minimum_observations`` calibration points are disabled,
    rather than borrowing another device's baseline.
    """

    center: np.ndarray
    scale: np.ndarray
    valid_entity: np.ndarray
    observation_count: np.ndarray
    minimum_observations: int = 3
    scale_floor: float = 0.15
    maximum_z: float = 20.0

    @classmethod
    def fit(
        cls,
        scores: np.ndarray,
        observed_mask: np.ndarray,
        *,
        minimum_observations: int = 3,
        scale_floor: float = 0.15,
        maximum_z: float = 20.0,
    ) -> "EntityScoreCalibrator":
        values, observed = _validate(scores, observed_mask)
        if values.shape[0] == 0:
            raise ValueError("entity calibration requires a non-empty time axis")
        if minimum_observations < 1 or scale_floor <= 0 or maximum_z <= 0:
            raise ValueError("entity calibration limits must be positive")
        if not np.isfinite(values[observed]).all():
            raise ValueError("observed entity scores must be finite")

        transformed = np.log1p(np.maximum(values, 0.0))
        entity_count = values.shape[1]
        center = np.zeros(entity_count, dtype=np.float64)
        scale = np.ones(entity_count, dtype=np.float64)
        counts = observed.sum(axis=0).astype(np.int64)
        valid = counts >= minimum_observations
        for entity in np.flatnonzero(valid):
            selected = transformed[observed[:, entity], entity]
            median = float(np.median(selected))
            mad = float(np.median(np.abs(selected - median)))
            q25, q75 = np.percentile(selected, [25.0, 75.0])
            center[entity] = median
            scale[entity] = max(1.4826 * mad, 0.5 * float(q75 - q25), scale_floor)
        return cls(
            center=center.astype(np.float32),
            scale=scale.astype(np.float32),
            valid_entity=valid,
            observation_count=counts,
            minimum_observations=minimum_observations,
            scale_floor=scale_floor,
            maximum_z=maximum_z,
        )

    def transform(self, scores: np.ndarray, observed_mask: np.ndarray) -> np.ndarray:
        values, observed = _validate(scores, observed_mask)
        if values.shape[1] != self.center.size:
            raise ValueError("entity score count does not match calibrator")
        if not np.isfinite(values[observed]).all():
            raise ValueError("observed entity scores must be finite")
        active = observed & self.valid_entity[None, :]
        transformed = np.log1p(np.maximum(np.where(active, values, 0.0), 0.0))
        normalized = np.maximum(
            (transformed - self.center[None, :]) / self.scale[None, :], 0.0
        )
        return np.where(active, np.minimum(normalized, self.maximum_z), 0.0).astype(np.float32)

    def to_dict(self) -> dict[str, Any]:
        return {
            "center": self.center.tolist(),
            "scale": self.scale.tolist(),
            "valid_entity": self.valid_entity.tolist(),
            "observation_count": self.observation_count.tolist(),
            "minimum_observations": self.minimum_observations,
            "scale_floor": self.scale_floor,
            "maximum_z": self.maximum_z,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EntityScoreCalibrator":
        return cls(
            center=np.asarray(value["center"], dtype=np.float32),
            scale=np.asarray(value["scale"], dtype=np.float32),
            valid_entity=np.asarray(value["valid_entity"], dtype=bool),
            observation_count=np.asarray(value["observation_count"], dtype=np.int64),
            minimum_observations=int(value.get("minimum_observations", 3)),
            scale_floor=float(value.get("scale_floor", 0.15)),
            maximum_z=float(value.get("maximum_z", 20.0)),
        )


def pool_entity_scores(
    scores: np.ndarray,
    observed_mask: np.ndarray,
    calibrator: EntityScoreCalibrator,
    *,
    allowed_entities: np.ndarray | None = None,
    evidence_gate: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Calibrate entity scores, then max-pool only eligible observed evidence."""
    normalized = calibrator.transform(scores, observed_mask)
    eligible = np.asarray(observed_mask, dtype=bool).copy()
    if allowed_entities is not None:
        allowed = np.asarray(allowed_entities, dtype=bool)
        if allowed.shape != (normalized.shape[1],):
            raise ValueError("allowed entity mask must match entity count")
        eligible &= allowed[None, :]
    if evidence_gate is not None:
        gate = np.asarray(evidence_gate, dtype=bool)
        if gate.shape != eligible.shape:
            raise ValueError("evidence gate must match [T, E] scores")
        eligible &= gate
    normalized = np.where(eligible, normalized, 0.0)
    return (
        normalized.max(axis=1, initial=0.0),
        eligible.any(axis=1),
        normalized,
    )


def calibrated_family_scores(
    *,
    node_scores: np.ndarray,
    edge_scores: np.ndarray,
    log_scores: np.ndarray,
    node_observed: np.ndarray,
    edge_observed: np.ndarray,
    log_observed: np.ndarray,
    calibrators: dict[str, EntityScoreCalibrator],
    traffic_edges: np.ndarray,
    service_gate: np.ndarray,
    node_gate: np.ndarray | None = None,
    log_gate: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Create [T, node/edge/log] triggers with NetFlow excluded.

    Neural node/log evidence is eligible only where direct semantic evidence
    supports that same node/minute. Traffic edges additionally require an
    independent request-aware quality gate. NetFlow never enters the edge pool.
    """
    node, _, _ = pool_entity_scores(
        node_scores, node_observed, calibrators["node"], evidence_gate=node_gate
    )
    edge, _, _ = pool_entity_scores(
        edge_scores,
        edge_observed,
        calibrators["edge"],
        allowed_entities=traffic_edges,
        evidence_gate=service_gate,
    )
    log, _, _ = pool_entity_scores(
        log_scores, log_observed, calibrators["log"], evidence_gate=log_gate
    )
    # A semantic gate controls the trigger value, not whether the telemetry
    # family is observable. Keeping quiet, gated-out minutes in calibration
    # prevents fitting a threshold only on already-selected candidates.
    node_valid = np.any(
        np.asarray(node_observed, dtype=bool)
        & calibrators["node"].valid_entity[None, :],
        axis=1,
    )
    edge_valid = np.any(
        np.asarray(edge_observed, dtype=bool)
        & np.asarray(traffic_edges, dtype=bool)[None, :]
        & calibrators["edge"].valid_entity[None, :],
        axis=1,
    )
    log_valid = np.any(
        np.asarray(log_observed, dtype=bool)
        & calibrators["log"].valid_entity[None, :],
        axis=1,
    )
    return (
        np.column_stack((node, edge, log)).astype(np.float32),
        np.column_stack((node_valid, edge_valid, log_valid)),
    )


def _validate(scores: np.ndarray, observed_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(scores, dtype=np.float64)
    observed = np.asarray(observed_mask, dtype=bool)
    if values.ndim != 2:
        raise ValueError("entity scores must have shape [T, E]")
    if observed.shape != values.shape:
        raise ValueError("mask shape must match entity scores")
    return values, observed

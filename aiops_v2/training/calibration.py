"""Robust conversion from reconstruction errors to event probabilities."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np


FAMILY_NAMES = ("node", "edge", "log")


@dataclass(frozen=True, slots=True)
class CalibrationSummary:
    family_names: tuple[str, ...]
    center: tuple[float, ...]
    scale: tuple[float, ...]
    valid_minute_count: tuple[int, ...]
    reference_minute_count: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "CalibrationSummary":
        return cls(
            family_names=tuple(str(item) for item in value["family_names"]),
            center=tuple(float(item) for item in value["center"]),
            scale=tuple(float(item) for item in value["scale"]),
            valid_minute_count=tuple(
                int(item) for item in value["valid_minute_count"]
            ),
            reference_minute_count=tuple(
                int(item) for item in value["reference_minute_count"]
            ),
        )


@dataclass(frozen=True, slots=True)
class AnomalyCalibrator:
    center: np.ndarray
    scale: np.ndarray
    threshold_z: float = 4.0
    temperature: float = 1.5
    scale_floor: float = 0.15
    summary: CalibrationSummary | None = None

    @classmethod
    def fit(
        cls,
        family_scores: np.ndarray,
        observed_mask: np.ndarray | None = None,
        *,
        reference_counts: tuple[int, ...] | None = None,
        valid_counts: tuple[int, ...] | None = None,
    ) -> "AnomalyCalibrator":
        values = np.asarray(family_scores, dtype=np.float64)
        if values.ndim != 2 or values.shape[0] < 3 or values.shape[1] != 3:
            raise ValueError("calibration requires at least three rows and three families")
        observed = _observation_mask(values, observed_mask)
        if not np.isfinite(values[observed]).all():
            raise ValueError("observed calibration scores must be finite")
        safe = np.where(observed, values, 0.0)
        transformed = np.log1p(np.maximum(safe, 0.0))
        center = np.zeros(3, dtype=np.float64)
        scale = np.ones(3, dtype=np.float64)
        observed_counts = observed.sum(axis=0).astype(np.int64)
        for family in range(3):
            selected = transformed[observed[:, family], family]
            if selected.size == 0:
                continue
            center[family] = np.median(selected)
            mad = np.median(np.abs(selected - center[family]))
            q25, q75 = np.percentile(selected, [25.0, 75.0])
            scale[family] = max(
                1.4826 * float(mad),
                0.5 * float(q75 - q25),
                abs(float(center[family])) * 0.01,
                0.15,
            )
        references = (
            tuple(int(item) for item in reference_counts)
            if reference_counts is not None
            else tuple(int(item) for item in observed_counts)
        )
        if len(references) != 3:
            raise ValueError("reference_counts must contain three family counts")
        valid_minute_counts = (
            tuple(int(item) for item in valid_counts)
            if valid_counts is not None
            else tuple(int(item) for item in observed_counts)
        )
        summary = CalibrationSummary(
            family_names=FAMILY_NAMES,
            center=tuple(float(item) for item in center),
            scale=tuple(float(item) for item in scale),
            valid_minute_count=valid_minute_counts,
            reference_minute_count=references,
        )
        return cls(center.astype(np.float32), scale.astype(np.float32), summary=summary)

    def transform(
        self,
        family_scores: np.ndarray,
        observed_mask: np.ndarray | None = None,
    ) -> np.ndarray:
        values = np.asarray(family_scores, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != self.center.shape[0]:
            raise ValueError("family score shape does not match calibrator")
        observed = _observation_mask(values, observed_mask)
        standardized = self.standardize(values, observed)
        ordered = np.sort(np.where(observed, standardized, -np.inf), axis=1)
        observed_counts = observed.sum(axis=1)
        global_score = np.zeros(values.shape[0], dtype=np.float64)
        multiple = observed_counts >= 2
        single = observed_counts == 1
        global_score[multiple] = (
            0.75 * ordered[multiple, -1] + 0.25 * ordered[multiple, -2]
        )
        global_score[single] = ordered[single, -1]
        logits = np.clip((global_score - self.threshold_z) / self.temperature, -40.0, 40.0)
        result = (1.0 / (1.0 + np.exp(-logits))).astype(np.float32)
        result[observed_counts == 0] = 0.0
        return result

    def standardize(
        self,
        family_scores: np.ndarray,
        observed_mask: np.ndarray | None = None,
    ) -> np.ndarray:
        values = np.asarray(family_scores, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != self.center.shape[0]:
            raise ValueError("family score shape does not match calibrator")
        observed = _observation_mask(values, observed_mask)
        if not np.isfinite(values[observed]).all():
            raise ValueError("observed family scores must be finite")
        safe = np.where(observed, values, 0.0)
        transformed = np.log1p(np.maximum(safe, 0.0))
        standardized = np.maximum((transformed - self.center) / self.scale, 0.0)
        return np.where(observed, standardized, 0.0).astype(np.float32)

    def to_dict(self) -> dict[str, Any]:
        return {
            "center": self.center.tolist(),
            "scale": self.scale.tolist(),
            "threshold_z": self.threshold_z,
            "temperature": self.temperature,
            "scale_floor": self.scale_floor,
            "summary": self.summary.to_dict() if self.summary is not None else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnomalyCalibrator":
        return cls(
            center=np.asarray(value["center"], dtype=np.float32),
            scale=np.asarray(value["scale"], dtype=np.float32),
            threshold_z=float(value.get("threshold_z", 4.0)),
            temperature=float(value.get("temperature", 1.5)),
            scale_floor=float(value.get("scale_floor", 0.15)),
            summary=(
                CalibrationSummary.from_dict(value["summary"])
                if value.get("summary") is not None
                else None
            ),
        )


def _observation_mask(values: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
    if mask is None:
        return np.ones(values.shape, dtype=bool)
    observed = np.asarray(mask, dtype=bool)
    if observed.shape != values.shape:
        raise ValueError("observation mask shape must match family scores")
    return observed

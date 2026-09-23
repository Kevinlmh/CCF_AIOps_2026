"""Robust conversion from reconstruction errors to event probabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True, slots=True)
class AnomalyCalibrator:
    center: np.ndarray
    scale: np.ndarray
    threshold_z: float = 4.0
    temperature: float = 1.5
    scale_floor: float = 0.15

    @classmethod
    def fit(cls, family_scores: np.ndarray) -> "AnomalyCalibrator":
        values = np.asarray(family_scores, dtype=np.float64)
        if values.ndim != 2 or values.shape[0] < 3 or values.shape[1] != 3:
            raise ValueError("calibration requires at least three rows and three families")
        if not np.isfinite(values).all():
            raise ValueError("calibration scores must be finite")
        transformed = np.log1p(np.maximum(values, 0.0))
        center = np.median(transformed, axis=0)
        mad = np.median(np.abs(transformed - center), axis=0)
        q25, q75 = np.percentile(transformed, [25.0, 75.0], axis=0)
        scale = np.maximum(1.4826 * mad, 0.5 * (q75 - q25))
        scale = np.maximum(scale, np.maximum(np.abs(center) * 0.01, 0.15))
        return cls(center.astype(np.float32), scale.astype(np.float32))

    def transform(self, family_scores: np.ndarray) -> np.ndarray:
        values = np.asarray(family_scores, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != self.center.shape[0]:
            raise ValueError("family score shape does not match calibrator")
        if not np.isfinite(values).all():
            raise ValueError("family scores must be finite")
        transformed = np.log1p(np.maximum(values, 0.0))
        standardized = np.maximum((transformed - self.center) / self.scale, 0.0)
        ordered = np.sort(standardized, axis=1)
        global_score = 0.75 * ordered[:, -1] + 0.25 * ordered[:, -2]
        logits = np.clip((global_score - self.threshold_z) / self.temperature, -40.0, 40.0)
        return (1.0 / (1.0 + np.exp(-logits))).astype(np.float32)

    def to_dict(self) -> dict[str, Any]:
        return {
            "center": self.center.tolist(),
            "scale": self.scale.tolist(),
            "threshold_z": self.threshold_z,
            "temperature": self.temperature,
            "scale_floor": self.scale_floor,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnomalyCalibrator":
        return cls(
            center=np.asarray(value["center"], dtype=np.float32),
            scale=np.asarray(value["scale"], dtype=np.float32),
            threshold_z=float(value.get("threshold_z", 4.0)),
            temperature=float(value.get("temperature", 1.5)),
            scale_floor=float(value.get("scale_floor", 0.15)),
        )

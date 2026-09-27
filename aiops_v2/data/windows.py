"""Robust scaling and fixed-length tensor windows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
import math
from typing import Any

import numpy as np

from aiops_v2.data.feature_store import FeatureStore


@dataclass(frozen=True, slots=True)
class RobustScaler:
    """Per-feature median/IQR scaler fitted only on observed cells.

    Old checkpoints omit ``transform_kind`` and retain linear scaling. New
    training may compress the robust z-score before it reaches the model.
    """

    center: np.ndarray
    scale: np.ndarray
    transform_kind: str = "linear"

    def __post_init__(self) -> None:
        if self.transform_kind not in {"linear", "asinh"}:
            raise ValueError(f"unsupported scaler transform: {self.transform_kind}")

    @classmethod
    def fit(
        cls, values: np.ndarray, mask: np.ndarray, *, transform_kind: str = "linear"
    ) -> "RobustScaler":
        if values.shape != mask.shape:
            raise ValueError("values and mask must have identical shapes")
        if values.ndim != 3:
            raise ValueError("scaler expects [time, entity, feature] arrays")
        entity_count, feature_count = values.shape[1:]
        center = np.zeros((entity_count, feature_count), dtype=np.float32)
        scale = np.ones((entity_count, feature_count), dtype=np.float32)
        for entity in range(entity_count):
            for feature in range(feature_count):
                observed = np.asarray(values[:, entity, feature])[
                    np.asarray(mask[:, entity, feature], dtype=bool)
                ]
                if observed.size == 0:
                    continue
                median = float(np.median(observed))
                q25, q75 = np.percentile(observed, [25.0, 75.0])
                robust_scale = float(q75 - q25)
                if not math.isfinite(robust_scale) or robust_scale < 1e-6:
                    robust_scale = max(abs(median) * 0.01, 1.0)
                center[entity, feature] = median
                scale[entity, feature] = robust_scale
        return cls(center=center, scale=scale, transform_kind=transform_kind)

    def transform(self, values: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if values.shape != mask.shape or values.shape[1:] != self.center.shape:
            raise ValueError("array shape does not match fitted scaler")
        result = (np.asarray(values, dtype=np.float32) - self.center) / self.scale
        result = np.where(mask, result, 0.0)
        if self.transform_kind == "asinh":
            result = np.clip(np.arcsinh(result), -20.0, 20.0)
        return np.asarray(result, dtype=np.float32)

    def to_dict(self) -> dict[str, Any]:
        return {
            "center": self.center.tolist(),
            "scale": self.scale.tolist(),
            "transform_kind": self.transform_kind,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RobustScaler":
        return cls(
            center=np.asarray(value["center"], dtype=np.float32),
            scale=np.asarray(value["scale"], dtype=np.float32),
            transform_kind=str(value.get("transform_kind", "linear")),
        )


def _padded_slice(array: np.ndarray, start: int, length: int) -> np.ndarray:
    stop = min(start + length, array.shape[0])
    selected = np.asarray(array[start:stop]).copy()
    if selected.shape[0] == length:
        return selected
    shape = (length,) + array.shape[1:]
    result = np.zeros(shape, dtype=array.dtype)
    result[: selected.shape[0]] = selected
    return result


def _time_features(start_time, start_index: int, length: int) -> np.ndarray:
    result = np.zeros((length, 4), dtype=np.float32)
    initial = start_time + timedelta(minutes=start_index)
    for offset in range(length):
        value = initial + timedelta(minutes=offset)
        minute_of_day = value.hour * 60 + value.minute
        day_phase = 2.0 * math.pi * minute_of_day / 1440.0
        minute_of_week = value.weekday() * 1440 + minute_of_day
        week_phase = 2.0 * math.pi * minute_of_week / 10080.0
        result[offset] = (
            math.sin(day_phase),
            math.cos(day_phase),
            math.sin(week_phase),
            math.cos(week_phase),
        )
    return result


def _graph_links(store: FeatureStore) -> np.ndarray:
    """Map directed observed edges to public target nodes for message passing."""
    node_indexes = {node: index for index, node in enumerate(store.entities.nodes)}
    links: list[tuple[int, int]] = []
    for edge_index, edge in enumerate(store.entities.edges):
        targets: tuple[str, ...] = ()
        if edge.relation == "traffic" and edge.target.startswith("service-group:"):
            _, city, _ = edge.target.split(":", 2)
            targets = tuple(
                f"{city}-service-vm-{instance}"
                for instance in (1, 2, 3)
            )
        elif edge.relation == "netflow" and edge.source in node_indexes:
            # The public NetFlow representation is node -> interface on that
            # node; it supports its source node but does not imply cross-node
            # causality.
            targets = (edge.source,)
        for target in targets:
            target_index = node_indexes.get(target)
            if target_index is not None:
                links.append((edge_index, target_index))
    if not links:
        return np.zeros((2, 0), dtype=np.int64)
    return np.asarray(links, dtype=np.int64).T


class WindowDataset:
    """Indexable fixed-window view over a feature store."""

    def __init__(
        self,
        store: FeatureStore,
        *,
        window_minutes: int = 120,
        stride_minutes: int = 30,
        scalers: Mapping[str, RobustScaler] | None = None,
    ) -> None:
        if window_minutes <= 0 or stride_minutes <= 0:
            raise ValueError("window and stride must be positive")
        self.store = store
        self.window_minutes = window_minutes
        self.stride_minutes = stride_minutes
        self.scalers = dict(scalers or {})
        self.graph_links = _graph_links(store)
        total = int(store.manifest["minute_count"])
        if total <= window_minutes:
            self.starts = (0,)
        else:
            starts = list(range(0, total - window_minutes + 1, stride_minutes))
            final = total - window_minutes
            if starts[-1] != final:
                starts.append(final)
            self.starts = tuple(starts)

    def __len__(self) -> int:
        return len(self.starts)

    def _modality(self, name: str, start: int) -> tuple[np.ndarray, np.ndarray]:
        values = _padded_slice(getattr(self.store, f"{name}_values"), start, self.window_minutes)
        mask = _padded_slice(getattr(self.store, f"{name}_mask"), start, self.window_minutes).astype(bool)
        scaler = self.scalers.get(name)
        if scaler is not None:
            values = scaler.transform(values, mask)
        else:
            values = np.where(mask, values, 0.0).astype(np.float32)
        return values, mask

    def __getitem__(self, item: int) -> dict[str, Any]:
        start = self.starts[item]
        node_x, node_mask = self._modality("node", start)
        edge_x, edge_mask = self._modality("edge", start)
        log_x, log_mask = self._modality("log", start)
        return {
            "node_x": node_x,
            "node_mask": node_mask,
            "edge_x": edge_x,
            "edge_mask": edge_mask,
            "log_x": log_x,
            "log_mask": log_mask,
            "time_x": _time_features(self.store.start_time, start, self.window_minutes),
            "graph_links": self.graph_links,
            "start_index": start,
            "start_time": self.store.start_time + timedelta(minutes=start),
        }

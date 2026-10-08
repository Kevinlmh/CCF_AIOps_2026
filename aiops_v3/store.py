"""Read-only, mask-aware access to a seven-source feature store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

import numpy as np

from .contracts import parse_time


class StoreError(ValueError):
    pass


@dataclass
class FeatureStore:
    path: Path
    manifest: dict
    node_values: np.ndarray
    node_mask: np.ndarray
    edge_values: np.ndarray
    edge_mask: np.ndarray
    log_values: np.ndarray
    log_mask: np.ndarray

    @property
    def nodes(self) -> list[str]:
        return self.manifest["entities"]["nodes"]

    @property
    def edges(self) -> list[dict]:
        return self.manifest["entities"]["edges"]

    @property
    def start(self) -> datetime:
        return parse_time(self.manifest["start_time"])

    def time_at(self, minute: int) -> datetime:
        return self.start + timedelta(minutes=minute)

    def feature_index(self, kind: str, name: str) -> int | None:
        try:
            return self.manifest["features"][kind].index(name)
        except ValueError:
            return None

    def observed_node(self, minute: int, node: int, feature: str) -> float | None:
        column = self.feature_index("node", feature)
        if column is None or not self.node_mask[minute, node, column]:
            return None
        value = float(self.node_values[minute, node, column])
        return value if np.isfinite(value) else None

    def iter_dimension_series(self, *, source: str | None = None,
                              node_id: str | None = None):
        """Expose the canonical per-peer/per-interface sidecar to inference."""
        if "dimension_series_count" not in self.manifest:
            return iter(())
        if not hasattr(self, "_canonical_store"):
            from .data.feature_store import FeatureStore as CanonicalFeatureStore
            self._canonical_store = CanonicalFeatureStore.open(self.path)
        return self._canonical_store.iter_dimension_series(source=source, node_id=node_id)

    def iter_text_events(self, *, start=None, end=None, node_ids=None):
        path = self.path / "text_evidence.jsonl"
        if not path.exists():
            return
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                timestamp = parse_time(item["timestamp"])
                if ((start is not None and timestamp < start) or
                        (end is not None and timestamp >= end) or
                        (node_ids is not None and item.get("node_id") not in node_ids)):
                    continue
                identity = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                yield {**item, "evidence_id": "text:" + hashlib.sha256(identity.encode()).hexdigest()[:24]}


def open_store(path: Path) -> FeatureStore:
    path = Path(path)
    try:
        manifest = json.loads((path / "manifest.json").read_text())
        version = manifest.get("format_version")
        if version is not None and (type(version) is not int or version not in {1, 2, 3, 4}):
            raise StoreError(f"unsupported feature store format version: {version}")
        arrays = {
            f"{kind}_{item}": np.load(path / f"{kind}_{item}.npy", mmap_mode="r", allow_pickle=False)
            for kind in ("node", "edge", "log")
            for item in ("values", "mask")
        }
        parse_time(manifest["start_time"])
        for kind in ("node", "edge", "log"):
            shape = tuple(manifest["shapes"][kind])
            if arrays[f"{kind}_values"].shape != shape or arrays[f"{kind}_mask"].shape != shape:
                raise StoreError(f"{kind} array shape differs from manifest")
            if version is not None and (arrays[f"{kind}_values"].dtype != np.float32 or arrays[f"{kind}_mask"].dtype != np.bool_):
                raise StoreError(f"{kind} array dtype differs from feature-store contract")
            if shape[0] != manifest["minute_count"] or shape[2] != len(manifest["features"][kind]):
                raise StoreError(f"{kind} manifest shape inconsistent")
        if arrays["node_values"].shape[1] != len(manifest["entities"]["nodes"]):
            raise StoreError("node entity count differs from shape")
        if arrays["edge_values"].shape[1] != len(manifest["entities"]["edges"]):
            raise StoreError("edge entity count differs from shape")
    except (OSError, KeyError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, StoreError):
            raise
        raise StoreError(f"invalid feature store {path}: {exc}") from exc
    return FeatureStore(path, manifest, **arrays)

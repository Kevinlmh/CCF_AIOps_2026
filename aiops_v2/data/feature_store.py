"""Disk-backed minute feature store built from canonical observations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np

from aiops_v2.contracts import EdgeKey, ProjectedFeature
from aiops_v2.data.features import FeatureRegistry
from aiops_v2.data.projection import project_observation
from aiops_v2.data.registry import EntityRegistry
from baseline.bian.preprocessing.observations import NumericObservation


_ARRAY_FILES = {
    "node_values": "node_values.npy",
    "node_mask": "node_mask.npy",
    "edge_values": "edge_values.npy",
    "edge_mask": "edge_mask.npy",
    "log_values": "log_values.npy",
    "log_mask": "log_mask.npy",
}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _edge_text(edge: EdgeKey) -> str:
    return json.dumps(edge.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _edge_from_text(value: str) -> EdgeKey:
    return EdgeKey.from_dict(json.loads(value))


class FeatureStore:
    """Read-only view over dense values and explicit observation masks."""

    def __init__(self, path: Path, manifest: dict[str, Any]) -> None:
        self.path = path
        self.manifest = manifest
        self.entities = EntityRegistry.from_dict(manifest["entities"])
        self.features = FeatureRegistry.from_dict(manifest["features"])
        for attribute, filename in _ARRAY_FILES.items():
            setattr(self, attribute, np.load(path / filename, mmap_mode="r"))

    @property
    def start_time(self) -> datetime:
        return _parse_iso(self.manifest["start_time"])

    @property
    def end_time(self) -> datetime:
        return _parse_iso(self.manifest["end_time"])

    @classmethod
    def open(cls, path: Path) -> "FeatureStore":
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format_version") != 1:
            raise ValueError("unsupported v2 feature store format")
        return cls(path, manifest)


def _prepare_destination(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"feature store destination is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _create_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE cells (
            modality TEXT NOT NULL,
            minute INTEGER NOT NULL,
            entity TEXT NOT NULL,
            feature TEXT NOT NULL,
            aggregation TEXT NOT NULL,
            sum_value REAL NOT NULL,
            count_value INTEGER NOT NULL,
            min_value REAL NOT NULL,
            max_value REAL NOT NULL,
            last_value REAL NOT NULL,
            PRIMARY KEY (modality, minute, entity, feature)
        ) WITHOUT ROWID
        """
    )
    return connection


_UPSERT = """
INSERT INTO cells VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
ON CONFLICT(modality, minute, entity, feature) DO UPDATE SET
    sum_value = cells.sum_value + excluded.sum_value,
    count_value = cells.count_value + 1,
    min_value = MIN(cells.min_value, excluded.min_value),
    max_value = MAX(cells.max_value, excluded.max_value),
    last_value = excluded.last_value
"""


def _cell_row(projected: ProjectedFeature) -> tuple[Any, ...]:
    minute = int(projected.minute.timestamp() // 60)
    entity = _edge_text(projected.entity) if isinstance(projected.entity, EdgeKey) else projected.entity
    return (
        projected.modality,
        minute,
        entity,
        projected.feature,
        projected.aggregation,
        projected.value,
        projected.value,
        projected.value,
        projected.value,
    )


def _cell_value(row: sqlite3.Row) -> float:
    aggregation = row[4]
    if aggregation == "sum":
        return float(row[5])
    if aggregation == "mean":
        return float(row[5]) / int(row[6])
    if aggregation == "min":
        return float(row[7])
    if aggregation == "max":
        return float(row[8])
    return float(row[9])


def _open_array(path: Path, shape: tuple[int, ...], dtype: str) -> np.memmap:
    result = np.lib.format.open_memmap(path, mode="w+", dtype=dtype, shape=shape)
    result[...] = 0
    return result


def build_feature_store(
    observations: Iterable[NumericObservation],
    network_config: Mapping[str, Any],
    destination: Path,
) -> FeatureStore:
    """Aggregate an observation stream on disk and materialize dense masked arrays."""
    _prepare_destination(destination)
    entities = EntityRegistry.from_network_config(network_config)
    features = FeatureRegistry()
    source_counts: Counter[str] = Counter()
    observation_count = 0
    projected_count = 0
    minimum_minute: int | None = None
    maximum_minute: int | None = None
    database = destination / "build.sqlite3"
    connection = _create_database(database)
    batch: list[tuple[Any, ...]] = []
    try:
        for observation in observations:
            observation_count += 1
            source_counts[observation.source] += 1
            for projected in project_observation(observation):
                projected_count += 1
                minute = int(projected.minute.timestamp() // 60)
                minimum_minute = minute if minimum_minute is None else min(minimum_minute, minute)
                maximum_minute = minute if maximum_minute is None else max(maximum_minute, minute)
                features.add(projected.modality, projected.feature)
                if isinstance(projected.entity, EdgeKey):
                    entities.add_edge(projected.entity)
                batch.append(_cell_row(projected))
                if len(batch) >= 4096:
                    connection.executemany(_UPSERT, batch)
                    batch.clear()
        if batch:
            connection.executemany(_UPSERT, batch)
        connection.commit()

        if minimum_minute is None or maximum_minute is None:
            raise ValueError("no projectable observations were found")
        entities.freeze()
        features.freeze()
        minute_count = maximum_minute - minimum_minute + 1
        node_shape = (minute_count, len(entities.nodes), len(features.names("node")))
        edge_shape = (minute_count, len(entities.edges), len(features.names("edge")))
        log_shape = (minute_count, len(entities.nodes), len(features.names("log")))
        node_values = _open_array(destination / _ARRAY_FILES["node_values"], node_shape, "float32")
        node_mask = _open_array(destination / _ARRAY_FILES["node_mask"], node_shape, "bool")
        edge_values = _open_array(destination / _ARRAY_FILES["edge_values"], edge_shape, "float32")
        edge_mask = _open_array(destination / _ARRAY_FILES["edge_mask"], edge_shape, "bool")
        log_values = _open_array(destination / _ARRAY_FILES["log_values"], log_shape, "float32")
        log_mask = _open_array(destination / _ARRAY_FILES["log_mask"], log_shape, "bool")

        cursor = connection.execute(
            "SELECT modality, minute, entity, feature, aggregation, "
            "sum_value, count_value, min_value, max_value, last_value FROM cells"
        )
        cell_count = 0
        for row in cursor:
            modality, minute, entity, feature = row[:4]
            time_index = int(minute) - minimum_minute
            value = _cell_value(row)
            if modality == "edge":
                entity_index = entities.edge_index(_edge_from_text(entity))
                feature_index = features.index("edge", feature)
                edge_values[time_index, entity_index, feature_index] = value
                edge_mask[time_index, entity_index, feature_index] = True
            elif modality == "log":
                entity_index = entities.node_index(entity)
                feature_index = features.index("log", feature)
                log_values[time_index, entity_index, feature_index] = value
                log_mask[time_index, entity_index, feature_index] = True
            else:
                entity_index = entities.node_index(entity)
                feature_index = features.index("node", feature)
                node_values[time_index, entity_index, feature_index] = value
                node_mask[time_index, entity_index, feature_index] = True
            cell_count += 1
        for array in (node_values, node_mask, edge_values, edge_mask, log_values, log_mask):
            array.flush()

        manifest = {
            "format_version": 1,
            "start_time": _iso(datetime.fromtimestamp(minimum_minute * 60, tz=timezone.utc)),
            "end_time": _iso(datetime.fromtimestamp(maximum_minute * 60, tz=timezone.utc)),
            "minute_count": minute_count,
            "observation_count": observation_count,
            "projected_count": projected_count,
            "cell_count": cell_count,
            "source_counts": dict(sorted(source_counts.items())),
            "parser_audit": _parser_audit(observations),
            "entities": entities.to_dict(),
            "features": features.to_dict(),
            "shapes": {
                "node": list(node_shape),
                "edge": list(edge_shape),
                "log": list(log_shape),
            },
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        connection.close()
    database.unlink(missing_ok=True)
    (destination / "build.sqlite3-shm").unlink(missing_ok=True)
    (destination / "build.sqlite3-wal").unlink(missing_ok=True)
    return FeatureStore.open(destination)


def _parser_audit(observations: Iterable[NumericObservation]) -> dict[str, Any] | None:
    """Capture parser counters when the input stream exposes them."""
    stats = getattr(observations, "stats", None)
    if stats is None:
        return None
    return {
        "files_by_source": dict(sorted(stats.files_by_source.items())),
        "rows_by_source": dict(sorted(stats.rows_by_source.items())),
        "bad_rows_by_source": dict(sorted(stats.bad_rows_by_source.items())),
        "warning_count": len(stats.warnings),
    }

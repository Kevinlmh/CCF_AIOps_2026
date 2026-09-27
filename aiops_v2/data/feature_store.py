"""Disk-backed minute feature store built from canonical observations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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
from baseline.bian.preprocessing.observations import NumericObservation, TextEvent


_ARRAY_FILES = {
    "node_values": "node_values.npy",
    "node_mask": "node_mask.npy",
    "edge_values": "edge_values.npy",
    "edge_mask": "edge_mask.npy",
    "log_values": "log_values.npy",
    "log_mask": "log_mask.npy",
}
_DIMENSION_SOURCES = {"interface", "routing", "scrape", "traffic"}
_DIMENSION_ARRAY = "dimension_cells.npy"
_DIMENSION_INDEX = "dimension_series.json"
_TEXT_EVENTS = "text_evidence.jsonl"
_BUILD_OUTPUT_FILES = {
    *_ARRAY_FILES.values(),
    _DIMENSION_ARRAY,
    _DIMENSION_INDEX,
    _TEXT_EVENTS,
    "manifest.json",
    "build.sqlite3",
    "build.sqlite3-shm",
    "build.sqlite3-wal",
    "build.sqlite3-journal",
}
_DIMENSION_DTYPE = np.dtype(
    [("time_index", "<u4"), ("series_id", "<u4"), ("value", "<f4")]
)


@dataclass(frozen=True, slots=True)
class DimensionSeries:
    """Sparse per-source series retained beside the dense model tensors."""

    source: str
    node_id: str
    metric: str
    dimensions: tuple[tuple[str, str], ...]
    direction: str
    aggregation: str
    time_indices: np.ndarray
    values: np.ndarray


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
        is_v2 = int(manifest.get("format_version", 1)) >= 2
        dimension_path = path / _DIMENSION_ARRAY
        if is_v2 and not dimension_path.exists():
            raise ValueError("v2 feature store is missing its dimension sidecar")
        self._dimension_cells = (
            (
                np.load(dimension_path, mmap_mode="r")
                if int(manifest.get("dimension_cell_count", 0)) > 0
                else np.empty((0,), dtype=_DIMENSION_DTYPE)
            )
            if is_v2
            else None
        )
        index_path = path / _DIMENSION_INDEX
        if is_v2 and not index_path.exists():
            raise ValueError("v2 feature store is missing its dimension series index")
        self._dimension_series = json.loads(index_path.read_text(encoding="utf-8")) if is_v2 else []
        if is_v2 and len(self._dimension_series) != int(manifest.get("dimension_series_count", -1)):
            raise ValueError("dimension sidecar series count does not match manifest")
        if is_v2 and self._dimension_cells is not None and len(self._dimension_cells) != int(
            manifest.get("dimension_cell_count", -1)
        ):
            raise ValueError("dimension sidecar cell count does not match manifest")
        text_path = path / _TEXT_EVENTS
        if is_v2 and not text_path.exists():
            raise ValueError("v2 feature store is missing its text evidence file")

    @property
    def start_time(self) -> datetime:
        return _parse_iso(self.manifest["start_time"])

    @property
    def end_time(self) -> datetime:
        return _parse_iso(self.manifest["end_time"])

    @classmethod
    def open(cls, path: Path) -> "FeatureStore":
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format_version") not in {1, 2}:
            raise ValueError("unsupported v2 feature store format")
        return cls(path, manifest)

    def iter_dimension_series(
        self,
        *,
        source: str | None = None,
        node_id: str | None = None,
    ) -> Iterable[DimensionSeries]:
        """Iterate sparse dimension series without loading the sidecar into RAM."""
        if self._dimension_cells is None:
            return
        for item in self._dimension_series:
            if source is not None and item["source"] != source:
                continue
            if node_id is not None and item["node_id"] != node_id:
                continue
            start = int(item["offset"])
            stop = start + int(item["count"])
            cells = self._dimension_cells[start:stop]
            yield DimensionSeries(
                source=item["source"],
                node_id=item["node_id"],
                metric=item["metric"],
                dimensions=tuple(tuple(pair) for pair in item["dimensions"]),
                direction=item["direction"],
                aggregation=item["aggregation"],
                time_indices=cells["time_index"],
                values=cells["value"],
            )

    def iter_text_events(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        node_ids: set[str] | None = None,
    ) -> Iterable[dict[str, Any]]:
        """Stream retained FRR and scrape text evidence, optionally time-filtered."""
        path = self.path / _TEXT_EVENTS
        if not path.exists():
            return
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = json.loads(line)
                timestamp = _parse_iso(event["timestamp"])
                if start is not None and timestamp < start:
                    continue
                if end is not None and timestamp >= end:
                    continue
                if node_ids is not None and event.get("node_id") not in node_ids:
                    continue
                yield event


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
    connection.execute(
        """
        CREATE TABLE dimension_series (
            series_id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            node_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            dimensions_json TEXT NOT NULL,
            direction TEXT NOT NULL,
            aggregation TEXT NOT NULL,
            UNIQUE (source, node_id, metric, dimensions_json, direction, aggregation)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE dimension_cells (
            series_id INTEGER NOT NULL,
            minute INTEGER NOT NULL,
            sum_value REAL NOT NULL,
            count_value INTEGER NOT NULL,
            min_value REAL NOT NULL,
            max_value REAL NOT NULL,
            last_value REAL NOT NULL,
            PRIMARY KEY (series_id, minute)
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

_DIMENSION_UPSERT = """
INSERT INTO dimension_cells VALUES (?, ?, ?, 1, ?, ?, ?)
ON CONFLICT(series_id, minute) DO UPDATE SET
    sum_value = dimension_cells.sum_value + excluded.sum_value,
    count_value = dimension_cells.count_value + 1,
    min_value = MIN(dimension_cells.min_value, excluded.min_value),
    max_value = MAX(dimension_cells.max_value, excluded.max_value),
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


def _dimension_series_key(observation: NumericObservation, aggregation: str) -> tuple[Any, ...]:
    dimensions = observation.dimensions
    # Routing state labels describe the observation's value, not a stable
    # peer/interface identity. Keep the numeric state transitions together.
    if observation.source == "routing" and observation.direction == "low":
        dimensions = tuple((key, value) for key, value in dimensions if key != "state")
    return (
        observation.source,
        observation.node_id,
        observation.metric,
        json.dumps(dimensions, ensure_ascii=True, separators=(",", ":")),
        observation.direction,
        aggregation,
    )


def _dimension_cell_value(
    aggregation: str,
    sum_value: float,
    count_value: int,
    min_value: float,
    max_value: float,
    last_value: float,
) -> float:
    if aggregation == "sum":
        return float(sum_value)
    if aggregation == "mean":
        return float(sum_value) / int(count_value)
    if aggregation == "min":
        return float(min_value)
    if aggregation == "max":
        return float(max_value)
    return float(last_value)


def _write_dimension_sidecar(
    connection: sqlite3.Connection,
    destination: Path,
    minimum_minute: int,
) -> tuple[int, int]:
    cell_count = int(connection.execute("SELECT COUNT(*) FROM dimension_cells").fetchone()[0])
    series_count = int(connection.execute("SELECT COUNT(*) FROM dimension_series").fetchone()[0])
    if cell_count:
        cells = np.lib.format.open_memmap(
            destination / _DIMENSION_ARRAY,
            mode="w+",
            dtype=_DIMENSION_DTYPE,
            shape=(cell_count,),
        )
    else:
        np.save(destination / _DIMENSION_ARRAY, np.empty((0,), dtype=_DIMENSION_DTYPE))
        cells = None
    metadata: list[dict[str, Any]] = []
    cursor = connection.execute(
        """
        SELECT s.series_id, s.source, s.node_id, s.metric, s.dimensions_json,
               s.direction, s.aggregation, c.minute, c.sum_value, c.count_value,
               c.min_value, c.max_value, c.last_value
        FROM dimension_series AS s
        JOIN dimension_cells AS c USING (series_id)
        ORDER BY s.series_id, c.minute
        """
    )
    current_id: int | None = None
    position = 0
    for row in cursor:
        series_id = int(row[0])
        if series_id != current_id:
            if current_id is not None:
                metadata[-1]["count"] = position - int(metadata[-1]["offset"])
            metadata.append(
                {
                    "series_id": series_id,
                    "source": row[1],
                    "node_id": row[2],
                    "metric": row[3],
                    "dimensions": json.loads(row[4]),
                    "direction": row[5],
                    "aggregation": row[6],
                    "offset": position,
                    "count": 0,
                }
            )
            current_id = series_id
        if cells is not None:
            cells[position] = (
                int(row[7]) - minimum_minute,
                series_id,
                _dimension_cell_value(row[6], row[8], row[9], row[10], row[11], row[12]),
            )
        position += 1
    if metadata:
        metadata[-1]["count"] = position - int(metadata[-1]["offset"])
    if position != cell_count or len(metadata) != series_count:
        raise RuntimeError("dimension sidecar materialization count mismatch")
    if cells is not None:
        cells.flush()
        del cells
    (destination / _DIMENSION_INDEX).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return series_count, cell_count


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
    dimension_batch: list[tuple[Any, ...]] = []
    dimension_ids: dict[tuple[Any, ...], int] = {}
    dimension_cell_count = 0
    text_event_count = 0
    text_path = destination / _TEXT_EVENTS
    build_succeeded = False
    try:
        with text_path.open("w", encoding="utf-8") as text_handle:
            drain_text = getattr(observations, "drain_text_events", None)
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
                    if (
                        observation.source in _DIMENSION_SOURCES
                        and observation.dimensions
                    ):
                        series_key = _dimension_series_key(
                            observation, projected.aggregation
                        )
                        series_id = dimension_ids.get(series_key)
                        if series_id is None:
                            series_id = len(dimension_ids)
                            dimension_ids[series_key] = series_id
                            connection.execute(
                                "INSERT INTO dimension_series VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (series_id, *series_key),
                            )
                        dimension_batch.append(
                            (
                                series_id,
                                minute,
                                projected.value,
                                projected.value,
                                projected.value,
                                projected.value,
                            )
                        )
                        if len(dimension_batch) >= 4096:
                            connection.executemany(_DIMENSION_UPSERT, dimension_batch)
                            dimension_cell_count += len(dimension_batch)
                            dimension_batch.clear()
                if callable(drain_text):
                    text_event_count += _write_text_events(text_handle, drain_text())
            if batch:
                connection.executemany(_UPSERT, batch)
            if dimension_batch:
                connection.executemany(_DIMENSION_UPSERT, dimension_batch)
                dimension_cell_count += len(dimension_batch)
            if callable(drain_text):
                text_event_count += _write_text_events(text_handle, drain_text())
            else:
                text_event_count += _write_text_events(
                    text_handle, getattr(observations, "text_events", ())
                )
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

        dimension_series_count, materialized_dimension_cells = _write_dimension_sidecar(
            connection, destination, minimum_minute
        )
        if materialized_dimension_cells != connection.execute(
            "SELECT COUNT(*) FROM dimension_cells"
        ).fetchone()[0]:
            raise RuntimeError("dimension sidecar cell count mismatch")
        if dimension_cell_count < materialized_dimension_cells:
            raise RuntimeError("dimension sidecar received fewer writes than stored cells")

        manifest = {
            "format_version": 2,
            "start_time": _iso(datetime.fromtimestamp(minimum_minute * 60, tz=timezone.utc)),
            "end_time": _iso(datetime.fromtimestamp(maximum_minute * 60, tz=timezone.utc)),
            "minute_count": minute_count,
            "observation_count": observation_count,
            "projected_count": projected_count,
            "cell_count": cell_count,
            "dimension_series_count": dimension_series_count,
            "dimension_cell_count": materialized_dimension_cells,
            "text_event_count": text_event_count,
            "dimension_sidecar": {
                "data_file": _DIMENSION_ARRAY,
                "index_file": _DIMENSION_INDEX,
                "dtype": {name: str(_DIMENSION_DTYPE[name]) for name in _DIMENSION_DTYPE.names},
                "sources": sorted(_DIMENSION_SOURCES),
            },
            "text_evidence_file": _TEXT_EVENTS,
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
        build_succeeded = True
    finally:
        connection.close()
        if not build_succeeded:
            for filename in _BUILD_OUTPUT_FILES:
                (destination / filename).unlink(missing_ok=True)
    database.unlink(missing_ok=True)
    (destination / "build.sqlite3-shm").unlink(missing_ok=True)
    (destination / "build.sqlite3-wal").unlink(missing_ok=True)
    return FeatureStore.open(destination)


def _parser_audit(observations: Iterable[NumericObservation]) -> dict[str, Any] | None:
    """Capture parser counters when the input stream exposes them."""
    stats = getattr(observations, "stats", None)
    if stats is None:
        return None
    sources = set(stats.bad_rows_by_source)
    sources.update(stats.invalid_numeric_values_by_source)
    sources.update(stats.out_of_order_rows_by_source)
    quality_issues = [
        {
            "source": source,
            "bad_rows": stats.bad_rows_by_source.get(source, 0),
            "invalid_numeric_values": stats.invalid_numeric_values_by_source.get(source, 0),
            "out_of_order_rows": stats.out_of_order_rows_by_source.get(source, 0),
        }
        for source in sorted(sources)
        if (
            stats.bad_rows_by_source.get(source, 0)
            or stats.invalid_numeric_values_by_source.get(source, 0)
            or stats.out_of_order_rows_by_source.get(source, 0)
        )
    ]
    return {
        "files_by_source": dict(sorted(stats.files_by_source.items())),
        "rows_by_source": dict(sorted(stats.rows_by_source.items())),
        "bad_rows_by_source": dict(sorted(stats.bad_rows_by_source.items())),
        "invalid_numeric_values_by_source": dict(
            sorted(stats.invalid_numeric_values_by_source.items())
        ),
        "unmapped_entities_by_source": dict(
            sorted(stats.unmapped_entities_by_source.items())
        ),
        "out_of_order_rows_by_source": dict(
            sorted(stats.out_of_order_rows_by_source.items())
        ),
        "timestamp_ranges_by_source": stats.timestamp_ranges_by_source,
        "file_audit": stats.file_audit,
        "quality_status": "issues_detected" if quality_issues else "clean",
        "quality_issues": quality_issues,
        "warning_count": len(stats.warnings),
        "warnings": list(stats.warnings),
    }


def _text_event_payload(event: TextEvent) -> dict[str, Any]:
    return {
        "timestamp": _iso(event.timestamp),
        "source": event.source,
        "node_id": event.node_id,
        "severity": event.severity,
        "program": event.program,
        "event_family": event.event_family,
        "message": event.message,
        "dimensions": [list(item) for item in event.dimensions],
    }


def _write_text_events(
    handle,
    events: Iterable[TextEvent],
) -> int:
    count = 0
    for event in events:
        handle.write(json.dumps(_text_event_payload(event), ensure_ascii=False) + "\n")
        count += 1
    return count

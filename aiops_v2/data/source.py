"""Bounded-memory adapters from the seven CSV families to canonical observations."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from baseline.bian.preprocessing.multisource import (
    IDENTITY_FIELDS,
    _counter_metric_name,
    _dimensions,
    _direction,
    _is_counter,
    _label_dimensions,
    _node_for_row,
    _number,
    _parse_frr_file,
    _parse_netflow_file,
    _read_rows,
    _row_time,
    _traffic_dimensions,
    _traffic_related_nodes,
    iter_source_files,
)
from baseline.bian.preprocessing.observations import (
    NumericObservation,
    ParseStats,
    city_from_path,
    normalize_node_id,
)


def _iter_dense(
    path: Path,
    source: str,
    city: str | None,
    roles: tuple[str, ...],
    stats: ParseStats,
) -> Iterator[NumericObservation]:
    for _, row in _read_rows(path, source, stats):
        timestamp = _row_time(row, source)
        node_id = _node_for_row(row, city, roles)
        if timestamp is None:
            stats.record_bad_row(source)
            continue
        if node_id is None:
            continue
        if source == "interface":
            dimensions = _dimensions(
                interface_id=row.get("interface_id"),
                if_role=row.get("if_role"),
            )
        elif source == "scrape":
            dimensions = _dimensions(
                target_id=row.get("target_id"),
                exporter_type=row.get("exporter_type"),
            )
        else:
            dimensions = ()
        if source == "routing":
            metric_name = (row.get("metric_name") or "value").strip().lower()
            value = _number(row.get("value"))
            if value is not None:
                yield NumericObservation(
                    timestamp=timestamp,
                    source=source,
                    node_id=node_id,
                    related_node_ids=(),
                    metric=f"routing.{metric_name}",
                    value=value,
                    dimensions=_label_dimensions(row.get("label")),
                    direction=_direction(source, metric_name),
                )
            continue
        for field_name, raw_value in row.items():
            if field_name in IDENTITY_FIELDS:
                continue
            value = _number(raw_value)
            if value is None:
                continue
            metric = f"{source}.{field_name}"
            yield NumericObservation(
                timestamp=timestamp,
                source=source,
                node_id=node_id,
                related_node_ids=(),
                metric=metric,
                value=value,
                dimensions=dimensions,
                direction=_direction(source, metric),
            )


def _iter_traffic(
    path: Path,
    roles: tuple[str, ...],
    stats: ParseStats,
) -> Iterator[NumericObservation]:
    previous: dict[tuple[str, str], tuple[datetime, float]] = {}
    for _, row in _read_rows(path, "traffic", stats):
        timestamp = _row_time(row, "traffic")
        flow_type = (row.get("flow_type") or "").strip().lower()
        source_city = (row.get("source_region") or "").strip().lower()
        target_city = (row.get("target_region") or "").strip().lower()
        if timestamp is None or not flow_type or not source_city or not target_city:
            stats.record_bad_row("traffic")
            continue
        source_node = normalize_node_id("traffic-vm", source_city, roles)
        if source_node is None:
            stats.record_bad_row("traffic")
            continue
        related = _traffic_related_nodes(target_city, roles)
        dimensions = _traffic_dimensions(row)
        prefix = f"{flow_type}_flow_"
        identity = row.get("series_key") or "|".join(value for _, value in dimensions)
        counts: dict[str, float] = {}
        reset = False
        for field_name, raw_value in row.items():
            if not field_name.startswith(prefix):
                continue
            value = _number(raw_value)
            if value is None:
                continue
            suffix = field_name[len(prefix) :]
            if _is_counter(suffix):
                key = (identity, field_name)
                old = previous.get(key)
                previous[key] = (timestamp, value)
                if old is None:
                    continue
                old_time, old_value = old
                elapsed = (timestamp - old_time).total_seconds() / 60.0
                if elapsed <= 0:
                    continue
                if value < old_value:
                    delta = value
                    reset = True
                else:
                    delta = value - old_value
                metric_suffix = _counter_metric_name(suffix)
                counts[metric_suffix] = delta
                metric_value = delta / elapsed
                metric = f"traffic.{flow_type}.{metric_suffix}"
            else:
                metric = f"traffic.{flow_type}.{suffix}"
                metric_value = value
            yield NumericObservation(
                timestamp=timestamp,
                source="traffic",
                node_id=source_node,
                related_node_ids=related,
                metric=metric,
                value=metric_value,
                dimensions=dimensions,
                direction=_direction("traffic", metric),
            )
        requests = counts.get("requests_rate")
        if requests is not None and requests > 0:
            for numerator, ratio in (
                ("success_rate", "success_ratio"),
                ("error_rate", "error_ratio"),
            ):
                count = counts.get(numerator)
                if count is None:
                    continue
                metric = f"traffic.{flow_type}.{ratio}"
                yield NumericObservation(
                    timestamp=timestamp,
                    source="traffic",
                    node_id=source_node,
                    related_node_ids=related,
                    metric=metric,
                    value=min(1.0, max(0.0, count / requests)),
                    dimensions=dimensions,
                    direction=_direction("traffic", metric),
                )
        if reset:
            yield NumericObservation(
                timestamp=timestamp,
                source="traffic",
                node_id=source_node,
                related_node_ids=related,
                metric=f"traffic.{flow_type}.counter_reset",
                value=1.0,
                dimensions=dimensions,
                direction="state",
            )


class CanonicalObservationStream:
    """Single-pass stream with parse statistics and bounded per-file state."""

    def __init__(
        self,
        root: Path,
        *,
        aliases: dict[str, str],
        valid_roles: Iterable[str],
    ) -> None:
        self.root = root
        self.aliases = dict(aliases)
        self.roles = tuple(valid_roles)
        self.stats = ParseStats()
        self._used = False

    def __iter__(self) -> Iterator[NumericObservation]:
        if self._used:
            raise RuntimeError("canonical observation stream is single-use")
        self._used = True
        for source, path in iter_source_files(self.root):
            self.stats.record_file(source)
            city = city_from_path(path, self.aliases)
            if source in {"node", "interface", "routing", "scrape"}:
                yield from _iter_dense(path, source, city, self.roles, self.stats)
            elif source == "traffic":
                yield from _iter_traffic(path, self.roles, self.stats)
            elif source == "netflow":
                yield from _parse_netflow_file(path, city, self.roles, self.stats)
            elif source == "frr":
                numeric, _ = _parse_frr_file(path, city, self.roles, self.stats)
                yield from numeric

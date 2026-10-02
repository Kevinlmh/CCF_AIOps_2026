"""Bounded-memory adapters from the seven CSV families to canonical observations."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from aiops_v3.data.multisource import (
    IDENTITY_FIELDS,
    _counter_metric_name,
    _dimensions,
    _direction,
    _is_counter,
    _label_dimensions,
    _node_for_row,
    _is_missing,
    _number,
    _parse_frr_file,
    _iter_netflow_file,
    _read_rows,
    _row_time,
    _traffic_dimensions,
    _traffic_related_nodes,
    iter_source_files,
)
from aiops_v3.data.observations import (
    NumericObservation,
    ParseStats,
    TextEvent,
    city_from_path,
    normalize_node_id,
)


def _iter_dense(
    path: Path,
    source: str,
    city: str | None,
    roles: tuple[str, ...],
    stats: ParseStats,
    text_events: list[TextEvent],
) -> Iterator[NumericObservation]:
    for _, row in _read_rows(path, source, stats):
        timestamp = _row_time(row, source, stats)
        node_id = _node_for_row(row, city, roles)
        if timestamp is None:
            stats.record_bad_row(source)
            continue
        if node_id is None:
            stats.record_unmapped_entity(source)
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
            elif not _is_missing(row.get("value")):
                stats.record_invalid_numeric(source)
            continue
        for field_name, raw_value in row.items():
            if field_name in IDENTITY_FIELDS:
                continue
            value = _number(raw_value)
            if value is None:
                if not _is_missing(raw_value):
                    stats.record_invalid_numeric(source)
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
        if source == "scrape":
            error = (row.get("scrape_error") or "").strip()
            if error and not _is_missing(error):
                text_events.append(
                    TextEvent(
                        timestamp=timestamp,
                        source="scrape",
                        node_id=node_id,
                        severity="error",
                        program=(row.get("exporter_type") or "exporter").strip(),
                        event_family="scrape_error",
                        message=error,
                        dimensions=dimensions,
                    )
                )


def _iter_traffic(
    path: Path,
    roles: tuple[str, ...],
    stats: ParseStats,
) -> Iterator[NumericObservation]:
    previous: dict[tuple[tuple[str, ...], str], tuple[datetime, float]] = {}
    previous_timestamp: dict[tuple[str, ...], datetime] = {}
    for _, row in _read_rows(path, "traffic", stats):
        timestamp = _row_time(row, "traffic", stats)
        flow_type = (row.get("flow_type") or "").strip().lower()
        source_city = (row.get("source_region") or "").strip().lower()
        target_city = (row.get("target_region") or "").strip().lower()
        if timestamp is None or not flow_type or not source_city or not target_city:
            stats.record_bad_row("traffic")
            continue
        source_node = normalize_node_id("traffic-vm", source_city, roles)
        if source_node is None:
            stats.record_unmapped_entity("traffic")
            continue
        related = _traffic_related_nodes(target_city, roles)
        dimensions = _traffic_dimensions(row)
        prefix = f"{flow_type}_flow_"
        identity = (
            source_city,
            target_city,
            flow_type,
            (row.get("target_domain") or "").strip().lower(),
            (row.get("series_key") or "|".join(value for _, value in dimensions)).strip(),
        )
        old_timestamp = previous_timestamp.get(identity)
        if old_timestamp is not None and timestamp <= old_timestamp:
            stats.record_out_of_order_row("traffic")
            continue
        previous_timestamp[identity] = timestamp
        counts: dict[str, float] = {}
        reset = False
        for field_name, raw_value in row.items():
            if not field_name.startswith(prefix):
                continue
            value = _number(raw_value)
            if value is None:
                if field_name.startswith(prefix) and not _is_missing(raw_value):
                    stats.record_invalid_numeric("traffic")
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
                if ratio == "error_ratio":
                    for suffix, amount in (
                        ("numerator", count), ("denominator", requests),
                    ):
                        yield NumericObservation(
                            timestamp=timestamp,
                            source="traffic",
                            node_id=source_node,
                            related_node_ids=related,
                            metric=f"traffic.{flow_type}.error_ratio_weighted_{suffix}",
                            value=amount,
                            dimensions=dimensions,
                            direction="high",
                        )
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
        profile: str = "stage1",
    ) -> None:
        self.root = root
        self.aliases = dict(aliases)
        self.roles = tuple(valid_roles)
        self.profile = profile
        self.stats = ParseStats()
        self.text_events: list[TextEvent] = []
        self._used = False

    def drain_text_events(self) -> tuple[TextEvent, ...]:
        """Release parsed text evidence incrementally while retaining bounded memory."""
        events = tuple(self.text_events)
        self.text_events.clear()
        return events

    def __iter__(self) -> Iterator[NumericObservation]:
        if self._used:
            raise RuntimeError("canonical observation stream is single-use")
        self._used = True
        for source, path in iter_source_files(self.root, profile=self.profile):
            self.stats.record_file(source, path)
            city = city_from_path(path, self.aliases)
            if source in {"node", "interface", "routing", "scrape"}:
                yield from _iter_dense(
                    path, source, city, self.roles, self.stats, self.text_events
                )
            elif source == "traffic":
                yield from _iter_traffic(path, self.roles, self.stats)
            elif source == "netflow":
                yield from _iter_netflow_file(path, city, self.roles, self.stats)
            elif source == "frr":
                numeric, text = _parse_frr_file(path, city, self.roles, self.stats)
                self.text_events.extend(text)
                yield from numeric

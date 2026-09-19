"""Parse every public observation family into a canonical bounded representation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import csv
import math
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Iterable, Iterator

from .observations import (
    NumericObservation,
    ParseStats,
    TextEvent,
    city_from_path,
    normalize_node_id,
    parse_time,
)


SOURCE_ORDER = ("node", "interface", "routing", "scrape", "traffic", "netflow", "frr")

TIME_FIELDS = {
    "node": ("timestamp",),
    "interface": ("timestamp",),
    "routing": ("timestamp",),
    "scrape": ("timestamp",),
    "traffic": ("timestamp_utc", "prometheus_sample_time_utc"),
    "netflow": ("minute_utc", "first_seen"),
    "frr": ("event_time", "received_at"),
}

IDENTITY_FIELDS = {
    "id",
    "timestamp",
    "timestamp_utc",
    "prometheus_sample_time_utc",
    "minute_utc",
    "region",
    "region_code",
    "node",
    "node_key",
    "node_type",
    "interface_id",
    "if_role",
    "target_id",
    "exporter_type",
    "metric_name",
    "label",
    "series_key",
    "flow_type",
    "source_region",
    "source_ip",
    "target_region",
    "target_domain",
    "protocol",
    "collector_port",
    "src_addr",
    "src_port",
    "dst_addr",
    "dst_port",
    "first_seen",
    "last_seen",
    "received_at",
    "event_time",
    "received_at_raw",
    "event_time_raw",
    "hostname",
    "facility",
    "severity",
    "severity_code",
    "program",
    "pid",
    "message",
    "inserted_at",
    "scrape_error",
}

NULL_VALUES = {"", "null", "none", "nan", "\\n", "\\N"}
LABEL_PATTERN = re.compile(r"([A-Za-z0-9_]+)\s*=\s*\"([^\"]*)\"")


@dataclass(frozen=True, slots=True)
class ObservationBundle:
    numeric: tuple[NumericObservation, ...]
    text_events: tuple[TextEvent, ...]
    stats: ParseStats
    source_coverage: dict[str, int]


def _source_for_file(path: Path) -> str | None:
    name = path.name.lower()
    if name.startswith("node_metrics"):
        return "node"
    if name.startswith("interface_metrics"):
        return "interface"
    if name.startswith("routing_metrics"):
        return "routing"
    if name.startswith("scrape_health"):
        return "scrape"
    if name == "traffic_flow_metrics.csv" or name.startswith("traffic_flow_metrics"):
        return "traffic"
    if name == "netflow_5tuple_minute_readable.csv" or name.startswith("netflow"):
        return "netflow"
    if name.startswith("frr_syslog_events"):
        return "frr"
    return None


def iter_source_files(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield official CSV files from sample and formal directory layouts."""
    found: list[tuple[str, Path]] = []
    for path in root.rglob("*.csv"):
        parent = path.parent.name.lower()
        if parent != "processed" and not parent.endswith("_data"):
            continue
        source = _source_for_file(path)
        if source is not None:
            found.append((source, path))
    order = {source: index for index, source in enumerate(SOURCE_ORDER)}
    yield from sorted(found, key=lambda item: (order[item[0]], item[1].as_posix()))


def validate_source_inventory(
    root: Path,
    aliases: dict[str, str],
    *,
    expected_cities: Iterable[str],
    expected_sources: Iterable[str] = SOURCE_ORDER,
) -> dict[str, dict[str, Path]]:
    """Return a complete city/source inventory or raise on gaps/duplicates."""
    cities = tuple(expected_cities)
    sources = tuple(expected_sources)
    inventory: dict[str, dict[str, Path]] = {city: {} for city in cities}
    for source, path in iter_source_files(root):
        city = city_from_path(path, aliases)
        if city not in inventory or source not in sources:
            continue
        if source in inventory[city]:
            raise ValueError(
                f"duplicate input for city={city} source={source}: "
                f"{inventory[city][source]} and {path}"
            )
        inventory[city][source] = path
    missing = [
        f"{city}/{source}"
        for city in cities
        for source in sources
        if source not in inventory[city]
    ]
    if missing:
        raise ValueError("missing required input: " + ", ".join(missing))
    return inventory


def _number(value: str | None) -> float | None:
    if value is None or value.strip() in NULL_VALUES or value.strip().lower() in NULL_VALUES:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _row_time(row: dict[str, str], source: str) -> datetime | None:
    for field_name in TIME_FIELDS[source]:
        result = parse_time(row.get(field_name))
        if result is not None:
            return result
    return None


def _dimensions(**values: str | None) -> tuple[tuple[str, str], ...]:
    return tuple(
        (key, value.strip())
        for key, value in values.items()
        if value is not None
        and value.strip()
        and value.strip() not in NULL_VALUES
        and value.strip().lower() not in NULL_VALUES
    )


def _label_dimensions(value: str | None) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    matches = tuple((key, item) for key, item in LABEL_PATTERN.findall(value))
    if matches:
        return matches
    clean = value.strip()
    return (("label", clean),) if clean and clean.lower() not in {"null", "none"} else ()


def _direction(source: str, metric: str) -> str:
    lower = metric.lower()
    if source == "node" and "memory_available" in lower:
        return "low"
    if source == "interface" and any(token in lower for token in ("drop", "error", "carrier")):
        return "high"
    if source == "routing" and any(
        token in lower for token in ("peer_up", "enabled", "route_exists", "command_success", "state_code")
    ):
        return "low"
    if source == "scrape":
        if "scrape_up" in lower or "samples" in lower:
            return "low"
        return "high"
    if source == "traffic":
        if any(token in lower for token in ("success_ratio", "throughput", "observed_qps")):
            return "low"
        if any(token in lower for token in ("error", "timeout", "latency", "loss", "retransmit", "jitter")):
            return "high"
    if source == "frr":
        return "high"
    return "both"


def _validate_header(source: str, fields: tuple[str, ...], path: Path) -> None:
    available = set(fields)
    requirements = {
        "node": ({"timestamp", "node"},),
        "interface": ({"timestamp", "node", "interface_id"},),
        "routing": ({"timestamp", "node", "metric_name", "value"},),
        "scrape": ({"timestamp", "node", "scrape_up"},),
        "traffic": ({"timestamp_utc", "flow_type", "source_region", "target_region"},),
        "netflow": ({"minute_utc", "packets", "bytes"},),
        "frr": ({"event_time", "message"},),
    }
    missing = sorted(requirements[source][0] - available)
    if source == "netflow" and not ({"node", "node_key"} & available):
        missing.append("node|node_key")
    if missing:
        raise ValueError(f"{source}: missing required fields {missing} in {path}")
    if source in {"node", "interface"} and not any(
        field not in IDENTITY_FIELDS for field in fields
    ):
        raise ValueError(f"{source}: missing numeric metric fields in {path}")
    if source == "traffic" and not any("_flow_" in field for field in fields):
        raise ValueError(f"traffic: missing flow metric fields in {path}")


def validate_source_file_header(path: Path, source: str) -> tuple[str, ...]:
    """Read and validate only the CSV header, without scanning the data body."""
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            reader = csv.reader(handle)
            fields = tuple(next(reader, ()))
    except OSError as exc:
        raise ValueError(f"{source}: cannot open {path}: {type(exc).__name__}") from exc
    if not fields:
        raise ValueError(f"{source}: missing header in {path}")
    _validate_header(source, fields, path)
    return fields


def _read_rows(path: Path, source: str, stats: ParseStats) -> Iterator[tuple[int, dict[str, str]]]:
    try:
        handle = path.open(newline="", encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        stats.warn(f"{source}: cannot open {path}: {type(exc).__name__}")
        return
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{source}: missing header in {path}")
        _validate_header(source, tuple(reader.fieldnames), path)
        for line_number, row in enumerate(reader, 2):
            stats.record_row(source)
            yield line_number, row


def _node_for_row(
    row: dict[str, str],
    city: str | None,
    valid_roles: tuple[str, ...],
) -> str | None:
    return normalize_node_id(row.get("node") or row.get("node_key"), city, valid_roles)


def _iter_dense_file(
    path: Path,
    source: str,
    city: str | None,
    valid_roles: tuple[str, ...],
    stats: ParseStats,
) -> Iterator[NumericObservation | TextEvent]:
    for _, row in _read_rows(path, source, stats):
        timestamp = _row_time(row, source)
        node_id = _node_for_row(row, city, valid_roles)
        if timestamp is None:
            stats.record_bad_row(source)
            continue
        stats.record_timestamp(source, timestamp)
        if node_id is None:
            # Public samples can contain explicitly excluded observation-only
            # roles such as probe-vm. They are valid rows, but cannot become a
            # root-cause candidate under the published element enumeration.
            stats.record_filtered_row(source)
            stats.record_unknown_node(source)
            continue
        stats.record_valid_row(source)

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

        emitted = 0
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
                emitted += 1
        else:
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
                emitted += 1

        if source == "scrape":
            error = (row.get("scrape_error") or "").strip()
            if error and error.lower() not in {"null", "none"} and error != "\\N":
                yield TextEvent(
                    timestamp=timestamp,
                    source="scrape",
                    node_id=node_id,
                    severity="error",
                    program=(row.get("exporter_type") or "exporter").strip(),
                    event_family="scrape_error",
                    message=error,
                )
                emitted += 1
        stats.record_emitted(source, emitted)
        # A long-form metric can legitimately be absent for one node/minute.
        # It is missing evidence, not a malformed CSV row.


def _parse_dense_file(
    path: Path,
    source: str,
    city: str | None,
    valid_roles: tuple[str, ...],
    stats: ParseStats,
) -> tuple[list[NumericObservation], list[TextEvent]]:
    numeric: list[NumericObservation] = []
    text: list[TextEvent] = []
    for item in _iter_dense_file(path, source, city, valid_roles, stats):
        if isinstance(item, NumericObservation):
            numeric.append(item)
        else:
            text.append(item)
    return numeric, text


def _traffic_dimensions(row: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return _dimensions(
        series_key=row.get("series_key"),
        flow_type=row.get("flow_type"),
        source_region=row.get("source_region"),
        target_region=row.get("target_region"),
        target_domain=row.get("target_domain"),
        protocol=row.get("protocol"),
    )


def _traffic_related_nodes(target_city: str, valid_roles: tuple[str, ...]) -> tuple[str, ...]:
    roles = tuple(role for role in valid_roles if role.startswith("service-vm-"))
    return tuple(f"{target_city}-{role}" for role in roles)


def _counter_metric_name(suffix: str) -> str:
    if suffix.endswith("_total"):
        return suffix[:-6] + "_rate"
    if suffix.endswith("_sum"):
        return suffix[:-4] + "_sum_rate"
    if suffix.endswith("_count"):
        return suffix[:-6] + "_count_rate"
    if "_bucket_le_" in suffix:
        return suffix.replace("_bucket_le_", "_bucket_rate_le_")
    return suffix + "_rate"


def _is_counter(suffix: str) -> bool:
    return suffix.endswith(("_total", "_sum", "_count")) or "_bucket_le_" in suffix


def _iter_traffic_file(
    path: Path,
    valid_roles: tuple[str, ...],
    stats: ParseStats,
    *,
    ratio_prior_weight: float = 0.0,
) -> Iterator[NumericObservation]:
    if ratio_prior_weight < 0:
        raise ValueError("traffic_ratio_prior_weight must be non-negative")
    previous: dict[tuple[str, str], tuple[datetime, float]] = {}
    for _, row in _read_rows(path, "traffic", stats):
        timestamp = _row_time(row, "traffic")
        flow_type = (row.get("flow_type") or "").strip().lower()
        source_city = (row.get("source_region") or "").strip().lower()
        target_city = (row.get("target_region") or "").strip().lower()
        if timestamp is None or not flow_type or not source_city or not target_city:
            stats.record_bad_row("traffic")
            continue
        stats.record_timestamp("traffic", timestamp)
        source_node = normalize_node_id("traffic-vm", source_city, valid_roles)
        if source_node is None:
            stats.record_filtered_row("traffic")
            stats.record_unknown_node("traffic")
            continue
        stats.record_valid_row("traffic")
        related = _traffic_related_nodes(target_city, valid_roles)
        dimensions = _traffic_dimensions(row)
        prefix = f"{flow_type}_flow_"
        identity = row.get("series_key") or "|".join(value for _, value in dimensions)
        row_deltas: dict[str, float] = {}
        row_counts: dict[str, float] = {}
        emitted = 0
        reset_detected = False
        for field_name, raw_value in row.items():
            if not field_name.startswith(prefix):
                continue
            value = _number(raw_value)
            if value is None:
                continue
            suffix = field_name[len(prefix) :]
            if _is_counter(suffix):
                state_key = (identity, field_name)
                old = previous.get(state_key)
                previous[state_key] = (timestamp, value)
                if old is None:
                    continue
                old_time, old_value = old
                elapsed_minutes = (timestamp - old_time).total_seconds() / 60.0
                if elapsed_minutes <= 0:
                    continue
                if value < old_value:
                    count_delta = value
                    reset_detected = True
                else:
                    count_delta = value - old_value
                metric_suffix = _counter_metric_name(suffix)
                row_counts[metric_suffix] = count_delta
                delta = count_delta / elapsed_minutes
                row_deltas[metric_suffix] = delta
                metric = f"traffic.{flow_type}.{metric_suffix}"
                metric_value = delta
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
            emitted += 1

        request_count = row_counts.get("requests_rate")
        if request_count is not None and request_count > 0:
            for numerator_name, ratio_name in (
                ("success_rate", "success_ratio"),
                ("error_rate", "error_ratio"),
            ):
                numerator_count = row_counts.get(numerator_name)
                if numerator_count is None:
                    continue
                prior_probability = 1.0 if ratio_name == "success_ratio" else 0.0
                adjusted_ratio = (
                    numerator_count + ratio_prior_weight * prior_probability
                ) / (request_count + ratio_prior_weight)
                metric = f"traffic.{flow_type}.{ratio_name}"
                yield NumericObservation(
                    timestamp=timestamp,
                    source="traffic",
                    node_id=source_node,
                    related_node_ids=related,
                    metric=metric,
                    value=min(1.0, max(0.0, adjusted_ratio)),
                    dimensions=dimensions + (("window_requests", str(request_count)),),
                    direction=_direction("traffic", metric),
                )
                emitted += 1
        if reset_detected:
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
            emitted += 1
        stats.record_emitted("traffic", emitted)


def _parse_traffic_file(
    path: Path,
    valid_roles: tuple[str, ...],
    stats: ParseStats,
    *,
    ratio_prior_weight: float = 0.0,
) -> list[NumericObservation]:
    return list(
        _iter_traffic_file(
            path,
            valid_roles,
            stats,
            ratio_prior_weight=ratio_prior_weight,
        )
    )


def _clean_token(value: str | None, default: str = "unknown") -> str:
    if value is None:
        return default
    clean = value.strip()
    if not clean or clean in NULL_VALUES or clean.lower() in NULL_VALUES:
        return default
    return clean


def _iter_netflow_file(
    path: Path,
    city: str | None,
    valid_roles: tuple[str, ...],
    stats: ParseStats,
    *,
    scratch_dir: Path | None = None,
) -> Iterator[NumericObservation]:
    """Aggregate unsorted NetFlow rows with a bounded on-disk SQLite spill."""
    insert_sql = "INSERT INTO flows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    if scratch_dir is not None:
        scratch_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="aiops_netflow_", dir=scratch_dir
    ) as directory:
        database = Path(directory) / "aggregate.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute(
                """
                CREATE TABLE flows (
                    minute TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    interface_id TEXT NOT NULL,
                    if_role TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    packets REAL NOT NULL,
                    bytes_value REAL NOT NULL,
                    flow_records REAL NOT NULL,
                    src_addr TEXT NOT NULL,
                    dst_addr TEXT NOT NULL,
                    src_port TEXT NOT NULL,
                    dst_port TEXT NOT NULL,
                    source_file TEXT NOT NULL
                )
                """
            )
            batch: list[tuple[object, ...]] = []
            for _, row in _read_rows(path, "netflow", stats):
                timestamp = _row_time(row, "netflow")
                node_id = _node_for_row(row, city, valid_roles)
                packets = _number(row.get("packets"))
                bytes_value = _number(row.get("bytes"))
                flow_records = _number(row.get("flow_record_count"))
                if timestamp is None or packets is None or bytes_value is None:
                    stats.record_bad_row("netflow")
                    continue
                stats.record_timestamp("netflow", timestamp)
                if node_id is None:
                    stats.record_filtered_row("netflow")
                    stats.record_unknown_node("netflow")
                    continue
                stats.record_valid_row("netflow")
                batch.append(
                    (
                        timestamp.isoformat(),
                        node_id,
                        _clean_token(row.get("interface_id")),
                        _clean_token(row.get("if_role")),
                        _clean_token(row.get("protocol")),
                        packets,
                        bytes_value,
                        flow_records or 0.0,
                        (row.get("src_addr") or "").strip(),
                        (row.get("dst_addr") or "").strip(),
                        (row.get("src_port") or "").strip(),
                        (row.get("dst_port") or "").strip(),
                        path.name,
                    )
                )
                if len(batch) >= 5000:
                    connection.executemany(insert_sql, batch)
                    batch.clear()
            if batch:
                connection.executemany(insert_sql, batch)
            connection.commit()

            rows = connection.execute(
                """
                WITH grouped AS (
                    SELECT
                        minute, node_id, interface_id, if_role, protocol,
                        SUM(packets) AS packets,
                        SUM(bytes_value) AS bytes_value,
                        SUM(flow_records) AS flow_records,
                        COUNT(DISTINCT NULLIF(src_addr, '')) AS unique_sources,
                        COUNT(DISTINCT NULLIF(dst_addr, '')) AS unique_destinations,
                        COUNT(DISTINCT NULLIF(src_port, '')) AS unique_source_ports,
                        COUNT(DISTINCT NULLIF(dst_port, '')) AS unique_destination_ports
                    FROM flows
                    GROUP BY minute, node_id, interface_id, if_role, protocol
                )
                SELECT
                    minute, node_id, interface_id, if_role, protocol,
                    packets, bytes_value, flow_records,
                    unique_sources, unique_destinations,
                    unique_source_ports, unique_destination_ports,
                    CASE
                        WHEN SUM(bytes_value) OVER (
                            PARTITION BY minute, node_id, interface_id, if_role
                        ) > 0
                        THEN bytes_value / SUM(bytes_value) OVER (
                            PARTITION BY minute, node_id, interface_id, if_role
                        )
                        ELSE 0.0
                    END AS protocol_byte_share
                FROM grouped
                ORDER BY minute, node_id, interface_id, if_role, protocol
                """
            )
            for row in rows:
                timestamp = parse_time(row[0])
                if timestamp is None:
                    raise ValueError(f"netflow aggregate emitted invalid timestamp: {row[0]}")
                dimensions = _dimensions(
                    interface_id=row[2],
                    if_role=row[3],
                    protocol=row[4],
                )
                values = {
                    "packets": row[5],
                    "bytes": row[6],
                    "flow_records": row[7],
                    "unique_sources": row[8],
                    "unique_destinations": row[9],
                    "unique_source_ports": row[10],
                    "unique_destination_ports": row[11],
                    "protocol_byte_share": row[12],
                }
                for suffix, value in values.items():
                    yield NumericObservation(
                        timestamp=timestamp,
                        source="netflow",
                        node_id=row[1],
                        related_node_ids=(),
                        metric=f"netflow.{suffix}",
                        value=float(value),
                        dimensions=dimensions,
                        direction="both",
                    )
                    stats.record_emitted("netflow")
        finally:
            connection.close()


def _parse_netflow_file(
    path: Path,
    city: str | None,
    valid_roles: tuple[str, ...],
    stats: ParseStats,
) -> list[NumericObservation]:
    return list(_iter_netflow_file(path, city, valid_roles, stats))


def _event_family(message: str, program: str) -> str:
    text = f"{program} {message}".lower()
    if "bgp" in text or "bgpd" in text:
        return "bgp"
    if "ospf" in text or "ospf6" in text:
        return "ospf"
    if any(token in text for token in ("route", "rib", "nexthop", "zebra")):
        return "route"
    if any(token in text for token in ("interface", "link", "carrier")):
        return "interface"
    if any(token in text for token in ("config", "command", "reload")):
        return "config"
    if any(token in text for token in ("process", "signal", "crash", "exit")):
        return "process"
    return "other"


def _iter_frr_file(
    path: Path,
    city: str | None,
    valid_roles: tuple[str, ...],
    stats: ParseStats,
) -> Iterator[NumericObservation | TextEvent]:
    counts: Counter[tuple[datetime, str | None, str, str, str]] = Counter()
    for _, row in _read_rows(path, "frr", stats):
        timestamp = _row_time(row, "frr")
        hostname = row.get("hostname") or row.get("node")
        node_id = normalize_node_id(hostname, city, valid_roles)
        message = row.get("message") or ""
        program = (row.get("program") or "frr").strip().lower()
        severity = (row.get("severity") or "unknown").strip().lower()
        if timestamp is None:
            stats.record_bad_row("frr")
            continue
        stats.record_timestamp("frr", timestamp)
        stats.record_valid_row("frr")
        if node_id is None:
            stats.record_unknown_node("frr")
        family = _event_family(message, program)
        event = TextEvent(
            timestamp=timestamp,
            source="frr",
            node_id=node_id,
            severity=severity,
            program=program,
            event_family=family,
            message=message,
        )
        yield event
        stats.record_emitted("frr")
        minute = timestamp.replace(second=0, microsecond=0)
        counts[(minute, node_id, severity, program, family)] += 1
    for (minute, node_id, severity, program, family), count in sorted(
        counts.items(), key=lambda item: (item[0][0], str(item[0][1]), item[0][4])
    ):
        yield NumericObservation(
            timestamp=minute,
            source="frr",
            node_id=node_id,
            related_node_ids=(),
            metric="frr.event_count",
            value=float(count),
            dimensions=_dimensions(severity=severity, program=program, event_family=family),
            direction="high",
        )
        stats.record_emitted("frr")


def _parse_frr_file(
    path: Path,
    city: str | None,
    valid_roles: tuple[str, ...],
    stats: ParseStats,
) -> tuple[list[NumericObservation], list[TextEvent]]:
    numeric: list[NumericObservation] = []
    text: list[TextEvent] = []
    for item in _iter_frr_file(path, city, valid_roles, stats):
        if isinstance(item, NumericObservation):
            numeric.append(item)
        else:
            text.append(item)
    return numeric, text


def iter_file_observations(
    path: Path,
    source: str,
    city: str | None,
    valid_roles: Iterable[str],
    stats: ParseStats,
    *,
    scratch_dir: Path | None = None,
    detector_config: dict[str, object] | None = None,
) -> Iterator[NumericObservation | TextEvent]:
    """Stream canonical observations for one recognized source file."""
    roles = tuple(valid_roles)
    if source in {"node", "interface", "routing", "scrape"}:
        yield from _iter_dense_file(path, source, city, roles, stats)
    elif source == "traffic":
        yield from _iter_traffic_file(
            path,
            roles,
            stats,
            ratio_prior_weight=float(
                (detector_config or {}).get("traffic_ratio_prior_weight", 0.0)
            ),
        )
    elif source == "netflow":
        yield from _iter_netflow_file(
            path, city, roles, stats, scratch_dir=scratch_dir
        )
    elif source == "frr":
        yield from _iter_frr_file(path, city, roles, stats)
    else:
        raise ValueError(f"unsupported source: {source}")


def load_observations(
    root: Path,
    aliases: dict[str, str],
    valid_roles: Iterable[str],
    *,
    detector_config: dict[str, object] | None = None,
) -> ObservationBundle:
    """Load all recognized public sources without retaining raw flow rows."""
    roles = tuple(valid_roles)
    numeric: list[NumericObservation] = []
    text_events: list[TextEvent] = []
    stats = ParseStats()
    discovered: set[str] = set()

    for source, path in iter_source_files(root):
        discovered.add(source)
        stats.record_file(source)
        city = city_from_path(path, aliases)
        if source in {"node", "interface", "routing", "scrape"}:
            values, text = _parse_dense_file(path, source, city, roles, stats)
            numeric.extend(values)
            text_events.extend(text)
        elif source == "traffic":
            numeric.extend(
                _parse_traffic_file(
                    path,
                    roles,
                    stats,
                    ratio_prior_weight=float(
                        (detector_config or {}).get(
                            "traffic_ratio_prior_weight", 0.0
                        )
                    ),
                )
            )
        elif source == "netflow":
            numeric.extend(_parse_netflow_file(path, city, roles, stats))
        elif source == "frr":
            values, text = _parse_frr_file(path, city, roles, stats)
            numeric.extend(values)
            text_events.extend(text)

    coverage = {source: stats.rows_by_source.get(source, 0) for source in SOURCE_ORDER if source in discovered}
    numeric.sort(key=lambda item: (item.timestamp, item.source, item.node_id or "", item.metric, item.dimensions))
    text_events.sort(key=lambda item: (item.timestamp, item.node_id or "", item.event_family))
    return ObservationBundle(tuple(numeric), tuple(text_events), stats, coverage)

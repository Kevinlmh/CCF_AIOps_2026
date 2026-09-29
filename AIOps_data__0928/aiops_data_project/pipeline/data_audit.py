#!/usr/bin/env python3
"""Reproducible seven-source AIOps data audit.

This module is deliberately read-only with respect to the raw dataset.  It
streams the six extracted CSV families and the NetFlow CSV members directly
from the original archives, then writes compact CSV/JSON audit artifacts.

The audit has three separate concerns:

* input reliability: time coverage, gaps, duplicates, parsing failures,
  missing-vs-zero semantics, counter behavior and entity coverage;
* entity/propagation provenance: which relationships are directly observed,
  derived from stable identifiers, or merely inferred;
* metric contracts: units, directions, counter transforms and evidence roles
  used by the v2 model.

It does not create normal/fault labels and does not change model predictions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import tarfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, TextIO, Tuple


UTC = timezone.utc
DATA_START = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)
DATA_END = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
EXPECTED_MINUTES = int((DATA_END - DATA_START).total_seconds() // 60)

NULL_TOKENS = {"", "null", "\\n", "\\N", "na", "n/a", "nan", "none"}
CITY_ALIASES = {
    "北大": "beida",
    "成都": "chengdu",
    "广州": "guangzhou",
    "南京": "nanjing",
    "上海": "shanghai",
    "沈阳": "shenyang",
    "武汉": "wuhan",
    "西安": "xian",
}
ROLES = (
    "br-1", "br-2", "cr-1", "cr-2", "fw", "traffic-vm",
    "service-vm-1", "service-vm-2", "service-vm-3", "monitor-vm",
)

SOURCE_SPECS: Dict[str, Dict[str, Any]] = {
    "node_metrics": {
        "timestamp": "timestamp",
        "grain": "one-minute node time series",
        "file_prefix": "node_metrics_",
        "entity_fields": ("region", "node", "node_type"),
        "numeric_fields": {
            "cpu_usage", "load1", "load5", "memory_available_ratio",
            "swap_used_ratio", "disk_read_rate", "disk_write_rate",
            "disk_io_util", "filesystem_used_ratio", "inode_used_ratio",
            "open_fd_ratio", "process_count",
        },
    },
    "interface_metrics": {
        "timestamp": "timestamp",
        "grain": "one-minute interface time series",
        "file_prefix": "interface_metrics_",
        "entity_fields": ("region", "node", "node_type", "interface_id", "if_role"),
        "numeric_fields": {
            "rx_bytes_rate", "tx_bytes_rate", "rx_packets_rate", "tx_packets_rate",
            "rx_drop_rate", "tx_drop_rate", "rx_error_rate", "tx_error_rate",
            "carrier_changes",
        },
    },
    "routing_metrics": {
        "timestamp": "timestamp",
        "grain": "one-minute routing metric series",
        "file_prefix": "routing_metrics_",
        "entity_fields": ("region", "node", "node_type", "metric_name", "label"),
        "numeric_fields": {"value"},
    },
    "scrape_health": {
        "timestamp": "timestamp",
        "grain": "one-minute exporter target series",
        "file_prefix": "scrape_health_",
        "entity_fields": ("region", "target_id", "node", "exporter_type"),
        "numeric_fields": {"scrape_up", "scrape_duration_seconds", "scrape_samples"},
    },
    "traffic_flow_metrics": {
        "timestamp": "timestamp_utc",
        "grain": "service flow series",
        "file_prefix": "traffic_flow_metrics",
        "entity_fields": ("source_region", "target_region", "target_domain", "flow_type", "protocol", "series_key"),
        "numeric_fields": None,
    },
    "frr_syslog_events": {
        "timestamp": "event_time",
        "grain": "irregular routing software event",
        "file_prefix": "frr_syslog_events_",
        "entity_fields": ("hostname", "region", "program", "severity"),
        "numeric_fields": {"id", "severity_code", "pid"},
    },
    "netflow_5tuple": {
        "timestamp": "minute_utc",
        "grain": "minute five-tuple flow record",
        "file_prefix": "netflow_5tuple_minute_readable.csv",
        "entity_fields": ("region", "region_code", "node_key", "node", "node_type", "interface_id", "if_role", "protocol", "src_addr", "dst_addr", "src_port", "dst_port"),
        "numeric_fields": {"collector_port", "protocol", "src_port", "dst_port", "packets", "bytes", "flow_record_count"},
    },
}

PER_MINUTE_SOURCES = {
    "node_metrics", "interface_metrics", "routing_metrics", "scrape_health",
    "traffic_flow_metrics", "netflow_5tuple",
}
COUNTER_SUFFIXES = ("_total", "_sum", "_count")
LABEL_RE = re.compile(r"([A-Za-z0-9_]+)\s*=\s*\"([^\"]*)\"")
ROLE_PATTERNS = (
    (re.compile(r"service[-_]?vm[-_]?([123])", re.I), lambda m: f"service-vm-{m.group(1)}"),
    (re.compile(r"traffic[-_]?vm", re.I), lambda _m: "traffic-vm"),
    (re.compile(r"monitor[-_]?vm", re.I), lambda _m: "monitor-vm"),
    (re.compile(r"(?:^|[-_])br[-_]?([12])(?:[-_]|$)", re.I), lambda m: f"br-{m.group(1)}"),
    (re.compile(r"(?:^|[-_])cr[-_]?([12])(?:[-_]|$)", re.I), lambda m: f"cr-{m.group(1)}"),
    (re.compile(r"(?:^|[-_])fw(?:[-_]|$)", re.I), lambda _m: "fw"),
)


def clean(value: Any) -> str:
    return str(value or "").strip().strip('"')


def is_missing(value: Any) -> bool:
    return clean(value).lower() in NULL_TOKENS


def parse_time(value: Any) -> Optional[datetime]:
    raw = clean(value)
    if not raw or raw.lower() in NULL_TOKENS:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_number(value: Any) -> Optional[float]:
    raw = clean(value)
    if not raw or raw.lower() in NULL_TOKENS:
        return None
    try:
        result = float(raw)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def city_from_dir(path: Path) -> str:
    match = re.match(r"^([^_]+)_20260819040000_20260902040000$", path.name)
    return match.group(1) if match else path.name.split("_2026", 1)[0]


def normalize_city(value: str, fallback: str = "") -> str:
    raw = clean(value).lower()
    if raw in CITY_ALIASES:
        return CITY_ALIASES[raw]
    for cn, en in CITY_ALIASES.items():
        if cn.lower() in raw:
            return en
    for city in CITY_ALIASES.values():
        if city in raw:
            return city
    return fallback


def normalize_role(value: str) -> Optional[str]:
    raw = clean(value).replace(" ", "")
    for pattern, converter in ROLE_PATTERNS:
        match = pattern.search(raw)
        if match:
            return converter(match)
    return None


def canonical_node(city: str, raw: str) -> Optional[str]:
    role = normalize_role(raw)
    return f"{city}-{role}" if role else None


def source_from_name(name: str) -> Optional[str]:
    name = name.lower()
    if name.startswith("node_metrics_"):
        return "node_metrics"
    if name.startswith("interface_metrics_"):
        return "interface_metrics"
    if name.startswith("routing_metrics_"):
        return "routing_metrics"
    if name.startswith("scrape_health_"):
        return "scrape_health"
    if name.startswith("frr_syslog_events_"):
        return "frr_syslog_events"
    if name == "traffic_flow_metrics.csv" or name.startswith("traffic_flow_metrics"):
        return "traffic_flow_metrics"
    if name == "netflow_5tuple_minute_readable.csv" or name.startswith("netflow_5tuple"):
        return "netflow_5tuple"
    return None


def discover_extracted(root: Path) -> List[Tuple[str, str, Path]]:
    result: List[Tuple[str, str, Path]] = []
    for path in sorted(root.rglob("*.csv")):
        if not path.parent.name.endswith("_data"):
            continue
        source = source_from_name(path.name)
        if source:
            result.append((city_from_dir(path.parent.parent), source, path))
    return result


def discover_archives(root: Path) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    consumed: set[Path] = set()
    for path in sorted(root.glob("*.tar.gz")):
        city = path.name.split("_2026", 1)[0]
        result.append({"city": city, "paths": [path], "storage": "archive"})
        consumed.add(path)
    for part_a in sorted(root.glob("*.tar.gz.aa")):
        base = Path(str(part_a)[:-3])
        part_b = Path(str(part_a)[:-2] + "ab")
        if part_b.exists():
            city = part_a.name.split("_2026", 1)[0]
            result.append({"city": city, "paths": [part_a, part_b], "storage": "split_archive"})
            consumed.update({part_a, part_b})
    return result


class ConcatenatedBinary:
    """Small sequential reader used by tarfile for split gzip archives."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self.paths = list(paths)
        self.index = 0
        self.handle: Optional[Any] = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        # Split gzip members are intentionally consumed sequentially.
        return False

    def writable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        pieces: List[bytes] = []
        remaining = size
        while self.index < len(self.paths) and (size < 0 or remaining > 0):
            if self.handle is None:
                self.handle = self.paths[self.index].open("rb")
            chunk_size = remaining if size >= 0 else 1024 * 1024
            chunk = self.handle.read(chunk_size)
            if chunk:
                pieces.append(chunk)
                if size >= 0:
                    remaining -= len(chunk)
                continue
            self.handle.close()
            self.handle = None
            self.index += 1
        return b"".join(pieces)

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class NonSeekableReader(io.RawIOBase):
    """Expose a tar member as a read-only raw stream without seeking."""

    def __init__(self, raw: Any) -> None:
        super().__init__()
        self.raw = raw

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def readinto(self, buffer: bytearray) -> int:
        data = self.raw.read(len(buffer))
        if not data:
            return 0
        buffer[:len(data)] = data
        return len(data)


def iter_archive_csv(archive: Dict[str, Any]) -> Iterator[Tuple[str, TextIO, Any]]:
    """Yield the NetFlow member as a text stream without extracting it."""
    paths: List[Path] = archive["paths"]
    city = archive["city"]
    stream: Optional[ConcatenatedBinary] = None
    if len(paths) == 1:
        tar = tarfile.open(paths[0], mode="r:gz")
    else:
        stream = ConcatenatedBinary(paths)
        tar = tarfile.open(fileobj=stream, mode="r|gz")
    try:
        for member in tar:
            if not member.isfile() or not member.name.endswith("netflow_5tuple_minute_readable.csv"):
                continue
            raw = tar.extractfile(member)
            if raw is None:
                continue
            if stream is None:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
            else:
                text = io.TextIOWrapper(io.BufferedReader(NonSeekableReader(raw)), encoding="utf-8-sig", errors="replace", newline="")
            yield city, text, member.name
            break
    finally:
        tar.close()
        if stream is not None:
            stream.close()


def iter_source_streams(root: Path, include_netflow: bool = True) -> Iterator[Tuple[str, str, str, TextIO, Optional[Path]]]:
    for city, source, path in discover_extracted(root):
        if source == "netflow_5tuple" and not include_netflow:
            continue
        handle = path.open("r", encoding="utf-8-sig", errors="replace", newline="")
        try:
            yield city, source, str(path), handle, path
        finally:
            handle.close()
    if include_netflow:
        extracted_cities = {city for city, source, _ in discover_extracted(root) if source == "netflow_5tuple"}
        for archive in discover_archives(root):
            if archive["city"] in extracted_cities:
                continue
            for city, handle, member in iter_archive_csv(archive):
                try:
                    yield city, "netflow_5tuple", f"{archive['paths'][0]}::{member}", handle, None
                finally:
                    handle.close()


def raw_node(row: Dict[str, str]) -> str:
    return clean(row.get("node") or row.get("node_key") or row.get("hostname"))


def entity_for(source: str, city: str, row: Dict[str, str]) -> Tuple[str, Optional[str], str]:
    if source == "traffic_flow_metrics":
        source_city = normalize_city(row.get("source_region", ""), city)
        return "traffic_observer", f"{source_city}-traffic-vm", "derived_from_source_region"
    if source == "frr_syslog_events":
        raw = clean(row.get("hostname"))
        node = canonical_node(city, raw)
        return "network_element", node, "derived_from_hostname" if node else "raw_hostname_unmapped"
    raw = raw_node(row)
    node = canonical_node(city, raw)
    return "network_element", node, "derived_from_node" if node else "raw_node_unmapped"


def series_key_for(source: str, city: str, row: Dict[str, str], entity: Optional[str]) -> str:
    if source == "node_metrics":
        return entity or raw_node(row)
    if source == "interface_metrics":
        return "|".join((entity or raw_node(row), clean(row.get("interface_id"))))
    if source == "routing_metrics":
        return "|".join((entity or raw_node(row), clean(row.get("metric_name")), clean(row.get("label"))))
    if source == "scrape_health":
        return "|".join((entity or raw_node(row), clean(row.get("target_id")), clean(row.get("exporter_type"))))
    if source == "traffic_flow_metrics":
        return clean(row.get("series_key")) or "|".join(
            clean(row.get(field)) for field in ("source_region", "target_region", "target_domain", "flow_type", "protocol")
        )
    if source == "netflow_5tuple":
        return "|".join((entity or raw_node(row), clean(row.get("interface_id"))))
    return "|".join((clean(row.get("hostname")), clean(row.get("program"))))


def semantic_key_for(source: str, city: str, row: Dict[str, str], entity: Optional[str], timestamp: Optional[datetime]) -> str:
    time_key = timestamp.isoformat() if timestamp else clean(row.get(SOURCE_SPECS[source]["timestamp"]))
    if source == "node_metrics":
        values = (time_key, entity or raw_node(row))
    elif source == "interface_metrics":
        values = (time_key, entity or raw_node(row), clean(row.get("interface_id")))
    elif source == "routing_metrics":
        values = (time_key, entity or raw_node(row), clean(row.get("metric_name")), clean(row.get("label")))
    elif source == "scrape_health":
        values = (time_key, entity or raw_node(row), clean(row.get("target_id")), clean(row.get("exporter_type")))
    elif source == "traffic_flow_metrics":
        values = (time_key, series_key_for(source, city, row, entity))
    elif source == "netflow_5tuple":
        values = (time_key, entity or raw_node(row), clean(row.get("interface_id")), clean(row.get("protocol")), clean(row.get("src_addr")), clean(row.get("src_port")), clean(row.get("dst_addr")), clean(row.get("dst_port")), clean(row.get("collector_port")))
    else:
        values = (time_key, clean(row.get("hostname")), clean(row.get("program")), clean(row.get("message")))
    return "|".join(values)


def row_signature(header: Sequence[str], row: Dict[str, str]) -> str:
    digest = hashlib.sha1()
    for field_name in header:
        digest.update(clean(row.get(field_name)).encode("utf-8", errors="replace"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def field_is_numeric(source: str, field_name: str) -> bool:
    spec = SOURCE_SPECS[source]
    numeric = spec["numeric_fields"]
    if numeric is not None:
        return field_name in numeric
    if source == "traffic_flow_metrics":
        if field_name == "id":
            return True
        return any(field_name.startswith(prefix) for prefix in ("dns_flow_", "web_flow_", "auth_flow_", "elephant_flow_"))
    return False


@dataclass
class FieldCounter:
    rows: int = 0
    missing: int = 0
    zero: int = 0
    numeric: int = 0
    invalid_numeric: int = 0
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    def add(self, source: str, field_name: str, raw: str) -> None:
        self.rows += 1
        if is_missing(raw):
            self.missing += 1
            return
        if not field_is_numeric(source, field_name):
            return
        value = parse_number(raw)
        if value is None:
            self.invalid_numeric += 1
            return
        self.numeric += 1
        if value == 0:
            self.zero += 1
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)


@dataclass
class FileAudit:
    city: str
    source: str
    path: str
    storage: str
    member: str = ""
    rows: int = 0
    columns: int = 0
    header: List[str] = field(default_factory=list)
    first: Optional[datetime] = None
    last: Optional[datetime] = None
    timestamp_parse_failures: int = 0
    malformed_rows: int = 0
    out_of_expected_range: int = 0
    backward_jumps: int = 0
    duplicate_exact_adjacent: int = 0
    duplicate_semantic_adjacent: int = 0
    observed_minutes: set[int] = field(default_factory=set)
    series_last: Dict[str, datetime] = field(default_factory=dict)
    series_rows: Counter = field(default_factory=Counter)
    series_gap_minutes: Counter = field(default_factory=Counter)
    series_gap_runs: Counter = field(default_factory=Counter)
    series_max_gap: Counter = field(default_factory=Counter)
    field_stats: Dict[str, FieldCounter] = field(default_factory=dict)
    entity_rows: Counter = field(default_factory=Counter)
    entity_minutes: Dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    entity_first: Dict[str, datetime] = field(default_factory=dict)
    entity_last: Dict[str, datetime] = field(default_factory=dict)
    previous_row_hash: str = ""
    previous_semantic_key: str = ""
    previous_counter_values: Dict[Tuple[str, str], Tuple[datetime, float]] = field(default_factory=dict)
    counter_stats: Dict[Tuple[str, str], Counter] = field(default_factory=lambda: defaultdict(Counter))

    def finish(self) -> Dict[str, Any]:
        observed = len(self.observed_minutes)
        if self.first is not None and self.last is not None:
            span_minutes = int((self.last - self.first).total_seconds() // 60) + 1
            internal_missing = max(0, span_minutes - observed)
        else:
            span_minutes = 0
            internal_missing = 0
        file_expected = EXPECTED_MINUTES if self.source in PER_MINUTE_SOURCES else None
        return {
            "city": self.city,
            "source": self.source,
            "path": self.path,
            "storage": self.storage,
            "member": self.member,
            "rows": self.rows,
            "columns": self.columns,
            "header": self.header,
            "first_timestamp_utc": self.first.isoformat().replace("+00:00", "Z") if self.first else None,
            "last_timestamp_utc": self.last.isoformat().replace("+00:00", "Z") if self.last else None,
            "span_minutes": span_minutes,
            "observed_minutes": observed,
            "expected_minutes_global": file_expected,
            "missing_minutes_global": max(0, file_expected - observed) if file_expected is not None else None,
            "missing_minutes_internal": internal_missing,
            "timestamp_parse_failures": self.timestamp_parse_failures,
            "malformed_rows": self.malformed_rows,
            "out_of_expected_range": self.out_of_expected_range,
            "backward_jumps": self.backward_jumps,
            "duplicate_exact_adjacent": self.duplicate_exact_adjacent,
            "duplicate_semantic_adjacent": self.duplicate_semantic_adjacent,
            "entity_count": len(self.entity_rows),
            "series_count": len(self.series_rows),
        }


def update_entity_registry(
    registry: Dict[Tuple[str, str], Dict[str, Any]],
    source: str,
    city: str,
    row: Dict[str, str],
    entity_type: str,
    entity_id: Optional[str],
    provenance: str,
) -> None:
    if not entity_id:
        return
    key = (entity_type, entity_id)
    if entity_type == "network_element":
        role = entity_id.split("-", 1)[1] if "-" in entity_id else entity_id
    else:
        role = ""
    item = registry.setdefault(key, {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "city": city,
        "role": role,
        "sources": set(),
        "raw_aliases": set(),
        "provenance": set(),
    })
    item["sources"].add(source)
    item["provenance"].add(provenance)
    for field_name in ("node", "node_key", "hostname", "target_id", "source_region", "target_region", "target_domain", "interface_id"):
        raw = clean(row.get(field_name))
        if raw:
            item["raw_aliases"].add(raw)


def add_relation(
    relations: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]],
    relation_type: str,
    left_type: str,
    left_id: str,
    right_type: str,
    right_id: str,
    city: str,
    source: str,
    raw_fields: str,
    provenance: str,
    confidence: str,
    reason: str,
) -> None:
    if not left_id or not right_id:
        return
    key = (relation_type, left_type, left_id, right_type, right_id, city)
    item = relations.setdefault(key, {
        "relation_type": relation_type,
        "left_type": left_type,
        "left_id": left_id,
        "right_type": right_type,
        "right_id": right_id,
        "city": city,
        "sources": set(),
        "raw_fields": set(),
        "provenance": set(),
        "confidence": confidence,
        "reason": reason,
    })
    item["sources"].add(source)
    item["raw_fields"].add(raw_fields)
    item["provenance"].add(provenance)


def parse_route_labels(label: str) -> Dict[str, str]:
    return {key: value for key, value in LABEL_RE.findall(clean(label))}


def update_relations(
    relations: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]],
    entity_registry: Dict[Tuple[str, str], Dict[str, Any]],
    source: str,
    city: str,
    row: Dict[str, str],
    entity_type: str,
    entity_id: Optional[str],
) -> None:
    if entity_id:
        update_entity_registry(entity_registry, source, city, row, entity_type, entity_id, "derived_from_raw_identifier")
    if source == "interface_metrics" and entity_id:
        interface_id = clean(row.get("interface_id"))
        if interface_id and not is_missing(interface_id):
            add_relation(relations, "owns_interface", "network_element", entity_id, "interface", f"{entity_id}:{interface_id}", city, source, "node|interface_id|if_role", "observed+derived", "high", "node and interface are in the same raw row")
    elif source == "frr_syslog_events" and entity_id:
        hostname = clean(row.get("hostname"))
        if hostname:
            add_relation(relations, "emits_frr_log", "network_element", entity_id, "frr_hostname", hostname, city, source, "hostname|event_time", "observed+derived", "high", "hostname role maps to canonical city-node")
        source_ip = clean(row.get("source_ip"))
        if source_ip:
            add_relation(relations, "has_observed_address", "network_element", entity_id, "ip_address", source_ip, city, source, "hostname|source_ip", "observed+derived", "high", "FRR source_ip is directly attached to hostname")
    elif source == "routing_metrics" and entity_id:
        labels = parse_route_labels(row.get("label", ""))
        for key in ("peer", "next_hop"):
            value = labels.get(key)
            if value:
                add_relation(relations, f"references_route_{key}", "network_element", entity_id, "ip_address", value, city, source, f"metric_name|label({key})", "observed", "medium", "routing label directly names a peer or next hop")
    elif source == "traffic_flow_metrics":
        source_city = normalize_city(row.get("source_region", ""), city)
        target_city = normalize_city(row.get("target_region", ""), "")
        target_domain = clean(row.get("target_domain"))
        observer = f"{source_city}-traffic-vm" if source_city else None
        if observer:
            add_relation(relations, "observes_service", "traffic_observer", observer, "service_target", target_domain or f"{target_city}:unknown", source_city, source, "source_region|target_region|target_domain|flow_type|protocol", "observed+derived", "high", "business probe source and target are explicit raw fields")
            if target_city:
                add_relation(relations, "targets_city", "service_target", target_domain or f"{target_city}:unknown", "city", target_city, source_city, source, "target_region|target_domain", "observed", "high", "target_region is explicit raw field")
    elif source == "netflow_5tuple" and entity_id:
        node_key = clean(row.get("node_key"))
        interface_id = clean(row.get("interface_id"))
        if node_key:
            add_relation(relations, "has_netflow_alias", "network_element", entity_id, "netflow_node_key", node_key, city, source, "node|node_key", "observed+derived", "high", "NetFlow node and node_key occur in the same raw record")
        if interface_id and not is_missing(interface_id):
            add_relation(relations, "observed_netflow_interface", "network_element", entity_id, "interface", f"{entity_id}:{interface_id}", city, source, "node|interface_id|if_role", "observed+derived", "high", "NetFlow interface occurs in the same raw record")


def counter_field(source: str, field_name: str, row: Dict[str, str]) -> bool:
    if source == "traffic_flow_metrics":
        flow_metric = any(field_name.startswith(prefix) for prefix in ("dns_flow_", "web_flow_", "auth_flow_", "elephant_flow_"))
        return flow_metric and (field_name.endswith(COUNTER_SUFFIXES) or "_bucket_le_" in field_name)
    if source == "routing_metrics":
        metric_name = clean(row.get("metric_name"))
        return field_name == "value" and (metric_name.endswith(COUNTER_SUFFIXES) or "_bucket_le_" in metric_name)
    return False


SEMANTIC_CONTRACTS: List[Dict[str, Any]] = [
    {"source": "node_metrics", "field": "cpu_usage", "kind": "gauge", "unit": "ratio_or_percent_to_verify", "direction": "high", "transform": "none_after_unit_validation", "role": "direct_resource_evidence", "zero": "idle_or_valid_zero", "missing": "no_observation"},
    {"source": "node_metrics", "field": "load1", "kind": "gauge", "unit": "load", "direction": "high", "transform": "none", "role": "direct_resource_evidence", "zero": "valid_low_load", "missing": "no_observation"},
    {"source": "node_metrics", "field": "load5", "kind": "gauge", "unit": "load", "direction": "high", "transform": "none", "role": "direct_resource_evidence", "zero": "valid_low_load", "missing": "no_observation"},
    {"source": "node_metrics", "field": "memory_available_ratio", "kind": "gauge", "unit": "ratio", "direction": "low", "transform": "none", "role": "direct_resource_evidence", "zero": "possible_exhaustion", "missing": "no_observation"},
    {"source": "node_metrics", "field": "disk_io_util", "kind": "gauge", "unit": "ratio_or_percent_to_verify", "direction": "high", "transform": "none_after_unit_validation", "role": "direct_resource_evidence", "zero": "idle_or_valid_zero", "missing": "no_observation"},
    {"source": "node_metrics", "field": "disk_read_rate", "kind": "gauge", "unit": "bytes_per_second", "direction": "context_dependent", "transform": "none", "role": "support_resource_evidence", "zero": "no_read_traffic_or_unavailable_to_verify", "missing": "no_observation"},
    {"source": "node_metrics", "field": "disk_write_rate", "kind": "gauge", "unit": "bytes_per_second", "direction": "context_dependent", "transform": "none", "role": "support_resource_evidence", "zero": "no_write_traffic_or_unavailable_to_verify", "missing": "no_observation"},
    {"source": "interface_metrics", "field": "rx_drop_rate", "kind": "gauge", "unit": "drops_per_second", "direction": "high", "transform": "none", "role": "direct_link_evidence", "zero": "no_observed_drops", "missing": "no_observation"},
    {"source": "interface_metrics", "field": "tx_drop_rate", "kind": "gauge", "unit": "drops_per_second", "direction": "high", "transform": "none", "role": "direct_link_evidence", "zero": "no_observed_drops", "missing": "no_observation"},
    {"source": "interface_metrics", "field": "rx_error_rate", "kind": "gauge", "unit": "errors_per_second", "direction": "high", "transform": "none", "role": "direct_link_evidence", "zero": "no_observed_errors", "missing": "no_observation"},
    {"source": "interface_metrics", "field": "tx_error_rate", "kind": "gauge", "unit": "errors_per_second", "direction": "high", "transform": "none", "role": "direct_link_evidence", "zero": "no_observed_errors", "missing": "no_observation"},
    {"source": "interface_metrics", "field": "carrier_changes", "kind": "count_or_counter_to_verify", "unit": "count", "direction": "high", "transform": "difference_if_cumulative", "role": "link_stability_evidence", "zero": "no_change", "missing": "no_observation"},
    {"source": "routing_metrics", "field": "bgp_peer_up", "kind": "state", "unit": "boolean", "direction": "low", "transform": "state_transition", "role": "direct_routing_evidence", "zero": "peer_down", "missing": "no_observation_not_up"},
    {"source": "routing_metrics", "field": "bgp_peer_prefix_received", "kind": "gauge", "unit": "prefixes", "direction": "low_or_change", "transform": "preserve_label_dimensions", "role": "routing_evidence", "zero": "no_prefixes_received", "missing": "no_observation"},
    {"source": "routing_metrics", "field": "ipv6_route_change_total", "kind": "counter", "unit": "route_changes", "direction": "high", "transform": "difference_with_reset_detection", "role": "routing_evidence", "zero": "no_change", "missing": "no_observation"},
    {"source": "scrape_health", "field": "scrape_up", "kind": "state_quality", "unit": "boolean", "direction": "low", "transform": "quality_mask", "role": "observability_quality_signal", "zero": "scrape_failed", "missing": "no_observation"},
    {"source": "scrape_health", "field": "scrape_samples", "kind": "gauge_quality", "unit": "samples", "direction": "low", "transform": "quality_context", "role": "observability_quality_signal", "zero": "no_samples", "missing": "no_observation"},
    {"source": "traffic_flow_metrics", "field": "*_latency_mean_seconds", "kind": "gauge", "unit": "seconds", "direction": "high", "transform": "none", "role": "service_symptom", "zero": "zero_latency_or_not_applicable_to_verify", "missing": "not_applicable_or_no_series"},
    {"source": "traffic_flow_metrics", "field": "*_latency_p95_seconds", "kind": "gauge", "unit": "seconds", "direction": "high", "transform": "none", "role": "service_symptom", "zero": "zero_latency_or_not_applicable_to_verify", "missing": "not_applicable_or_no_series"},
    {"source": "traffic_flow_metrics", "field": "*_loss_rate", "kind": "gauge", "unit": "ratio", "direction": "high", "transform": "none", "role": "service_or_link_symptom", "zero": "no_observed_loss", "missing": "not_applicable_or_no_series"},
    {"source": "traffic_flow_metrics", "field": "*_failed_total", "kind": "counter", "unit": "count", "direction": "high", "transform": "difference_then_rate_or_ratio", "role": "service_symptom", "zero": "no_failed_requests", "missing": "not_applicable_or_no_series"},
    {"source": "traffic_flow_metrics", "field": "*_timeout_total", "kind": "counter", "unit": "count", "direction": "high", "transform": "difference_then_rate_or_ratio", "role": "service_symptom", "zero": "no_timeouts", "missing": "not_applicable_or_no_series"},
    {"source": "traffic_flow_metrics", "field": "*_requests_total", "kind": "counter", "unit": "count", "direction": "context", "transform": "difference_to_request_rate", "role": "denominator_context", "zero": "no_requests", "missing": "not_applicable_or_no_series"},
    {"source": "netflow_5tuple", "field": "packets", "kind": "bucket_measure", "unit": "packets_per_minute", "direction": "context_dependent", "transform": "aggregate_by_entity_interface_protocol", "role": "auxiliary_flow_evidence", "zero": "no_packets_in_bucket", "missing": "no_record"},
    {"source": "netflow_5tuple", "field": "bytes", "kind": "bucket_measure", "unit": "bytes_per_minute", "direction": "context_dependent", "transform": "aggregate_by_entity_interface_protocol", "role": "auxiliary_flow_evidence", "zero": "no_bytes_in_bucket", "missing": "no_record"},
    {"source": "netflow_5tuple", "field": "flow_record_count", "kind": "bucket_measure", "unit": "records_per_minute", "direction": "context_dependent", "transform": "aggregate_by_entity_interface_protocol", "role": "auxiliary_flow_evidence", "zero": "no_records", "missing": "no_record"},
    {"source": "frr_syslog_events", "field": "event_time", "kind": "event_time", "unit": "UTC", "direction": "n_a", "transform": "join_key", "role": "routing_event_evidence", "zero": "n_a", "missing": "invalid_event_time"},
]


def write_csv(path: Path, rows: Iterable[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def json_ready(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Counter):
        return dict(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


def audit_file(
    city: str,
    source: str,
    path: str,
    handle: TextIO,
    storage: str,
    member: str,
    entity_registry: Dict[Tuple[str, str], Dict[str, Any]],
    relations: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]],
) -> FileAudit:
    audit = FileAudit(city, source, path, storage, member)
    reader = csv.DictReader(handle)
    header = list(reader.fieldnames or [])
    audit.header = header
    audit.columns = len(header)
    audit.field_stats = {field_name: FieldCounter() for field_name in header}
    timestamp_field = SOURCE_SPECS[source]["timestamp"]
    previous_file_timestamp: Optional[datetime] = None
    for row in reader:
        audit.rows += 1
        if None in row or any(value is None for value in row.values()):
            audit.malformed_rows += 1
        for field_name in header:
            audit.field_stats[field_name].add(source, field_name, row.get(field_name, ""))
        timestamp = parse_time(row.get(timestamp_field, ""))
        if timestamp is None:
            if not is_missing(row.get(timestamp_field, "")):
                audit.timestamp_parse_failures += 1
        else:
            audit.first = timestamp if audit.first is None else min(audit.first, timestamp)
            audit.last = timestamp if audit.last is None else max(audit.last, timestamp)
            if timestamp < DATA_START or timestamp >= DATA_END:
                audit.out_of_expected_range += 1
            if previous_file_timestamp is not None and timestamp < previous_file_timestamp:
                audit.backward_jumps += 1
            previous_file_timestamp = timestamp
            minute = int(timestamp.timestamp() // 60)
            audit.observed_minutes.add(minute)
        entity_type, entity_id, provenance = entity_for(source, city, row)
        update_relations(relations, entity_registry, source, city, row, entity_type, entity_id)
        if entity_id:
            audit.entity_rows[entity_id] += 1
            if timestamp is not None:
                minute = int(timestamp.timestamp() // 60)
                audit.entity_minutes[entity_id].add(minute)
                audit.entity_first[entity_id] = timestamp if entity_id not in audit.entity_first else min(audit.entity_first[entity_id], timestamp)
                audit.entity_last[entity_id] = timestamp if entity_id not in audit.entity_last else max(audit.entity_last[entity_id], timestamp)
        series_key = series_key_for(source, city, row, entity_id)
        audit.series_rows[series_key] += 1
        if timestamp is not None and source in PER_MINUTE_SOURCES:
            previous_series_time = audit.series_last.get(series_key)
            if previous_series_time is not None:
                delta_minutes = (timestamp - previous_series_time).total_seconds() / 60.0
                if delta_minutes > 1.0:
                    missing = int(math.floor(delta_minutes)) - 1
                    audit.series_gap_minutes[series_key] += missing
                    audit.series_gap_runs[series_key] += 1
                    audit.series_max_gap[series_key] = max(audit.series_max_gap[series_key], missing)
                elif delta_minutes < 0:
                    audit.backward_jumps += 1
            audit.series_last[series_key] = timestamp
        exact_signature = row_signature(header, row)
        semantic_signature = semantic_key_for(source, city, row, entity_id, timestamp)
        if exact_signature == audit.previous_row_hash:
            audit.duplicate_exact_adjacent += 1
        if semantic_signature == audit.previous_semantic_key:
            audit.duplicate_semantic_adjacent += 1
        audit.previous_row_hash = exact_signature
        audit.previous_semantic_key = semantic_signature

        if timestamp is not None:
            for field_name in header:
                if not counter_field(source, field_name, row):
                    continue
                value = parse_number(row.get(field_name, ""))
                if value is None:
                    continue
                counter_key = (series_key, field_name)
                previous = audit.previous_counter_values.get(counter_key)
                if previous is not None:
                    previous_time, previous_value = previous
                    delta = value - previous_value
                    stats = audit.counter_stats[counter_key]
                    stats["observations"] += 1
                    if delta == 0:
                        stats["repeats"] += 1
                    elif delta > 0:
                        stats["positive_deltas"] += 1
                    else:
                        stats["negative_deltas"] += 1
                        stats["resets_or_wraps"] += 1
                    elapsed = (timestamp - previous_time).total_seconds()
                    if elapsed <= 0:
                        stats["non_positive_time_intervals"] += 1
                audit.previous_counter_values[counter_key] = (timestamp, value)
    return audit


def merge_entity_registry(
    target: Dict[Tuple[str, str], Dict[str, Any]],
    incoming: Dict[Tuple[str, str], Dict[str, Any]],
) -> None:
    """Merge one worker's provenance registry without losing raw aliases."""
    for key, item in incoming.items():
        current = target.setdefault(key, {
            "entity_type": item["entity_type"],
            "entity_id": item["entity_id"],
            "city": item["city"],
            "role": item["role"],
            "sources": set(),
            "raw_aliases": set(),
            "provenance": set(),
        })
        current["sources"].update(item["sources"])
        current["raw_aliases"].update(item["raw_aliases"])
        current["provenance"].update(item["provenance"])


def merge_relations(
    target: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]],
    incoming: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]],
) -> None:
    """Merge one worker's relation evidence while preserving provenance sets."""
    for key, item in incoming.items():
        current = target.setdefault(key, {
            "relation_type": item["relation_type"],
            "left_type": item["left_type"],
            "left_id": item["left_id"],
            "right_type": item["right_type"],
            "right_id": item["right_id"],
            "city": item["city"],
            "sources": set(),
            "raw_fields": set(),
            "provenance": set(),
            "confidence": item["confidence"],
            "reason": item["reason"],
        })
        current["sources"].update(item["sources"])
        current["raw_fields"].update(item["raw_fields"])
        current["provenance"].update(item["provenance"])


def audit_netflow_archive_worker(archive: Dict[str, Any]) -> Dict[str, Any]:
    """Audit one compressed NetFlow member in an isolated process.

    NetFlow members are several GB per city after decompression.  The worker
    keeps the exact same row-level audit logic as extracted CSVs, but lets
    independent cities use separate CPU cores and returns only the compact
    per-city state needed by the final merge.
    """
    registry: Dict[Tuple[str, str], Dict[str, Any]] = {}
    relations: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]] = {}
    for city, handle, member in iter_archive_csv(archive):
        try:
            audit = audit_file(
                city,
                "netflow_5tuple",
                f"{archive['paths'][0]}::{member}",
                handle,
                "compressed_stream",
                member,
                registry,
                relations,
            )
        finally:
            handle.close()
        return {"audit": audit, "registry": registry, "relations": relations}
    raise RuntimeError(f"NetFlow member not found for {archive['city']}")


def run_audit(
    root: Path,
    output_dir: Path,
    include_netflow: bool = True,
    netflow_workers: Optional[int] = None,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    file_audits: List[FileAudit] = []
    entity_registry: Dict[Tuple[str, str], Dict[str, Any]] = {}
    relations: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]] = {}
    for index, (city, source, path, handle, local_path) in enumerate(iter_source_streams(root, include_netflow=False), 1):
        storage = "extracted" if local_path is not None else "compressed_stream"
        member = "" if local_path is not None else path.split("::", 1)[1]
        print(f"[audit {index}] scanning {city} {source} ({storage})", flush=True)
        file_audits.append(audit_file(city, source, path, handle, storage, member, entity_registry, relations))

    netflow_archives = discover_archives(root) if include_netflow else []
    if netflow_archives:
        workers = netflow_workers or min(4, max(1, os.cpu_count() or 1))
        workers = max(1, min(workers, len(netflow_archives)))
        print(f"[audit] parallel NetFlow workers={workers} cities={len(netflow_archives)}", flush=True)
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(audit_netflow_archive_worker, archive): archive for archive in netflow_archives}
            for offset, future in enumerate(as_completed(futures), len(file_audits) + 1):
                archive = futures[future]
                result = future.result()
                file_audits.append(result["audit"])
                merge_entity_registry(entity_registry, result["registry"])
                merge_relations(relations, result["relations"])
                print(f"[audit {offset}] complete {archive['city']} netflow_5tuple (compressed_stream)", flush=True)

    file_rows = [audit.finish() for audit in file_audits]
    quality_rows: List[Dict[str, Any]] = []
    for item in file_audits:
        finished = item.finish()
        quality_rows.append({
            "city": item.city,
            "source": item.source,
            "storage": item.storage,
            "path": item.path,
            "member": item.member,
            "rows": item.rows,
            "first_timestamp_utc": finished["first_timestamp_utc"],
            "last_timestamp_utc": finished["last_timestamp_utc"],
            "span_minutes": finished["span_minutes"],
            "observed_minutes": finished["observed_minutes"],
            "expected_minutes_global": finished["expected_minutes_global"],
            "missing_minutes_global": finished["missing_minutes_global"],
            "missing_minutes_internal": finished["missing_minutes_internal"],
            "timestamp_parse_failures": item.timestamp_parse_failures,
            "malformed_rows": item.malformed_rows,
            "out_of_expected_range": item.out_of_expected_range,
            "backward_jumps": item.backward_jumps,
            "duplicate_exact_adjacent": item.duplicate_exact_adjacent,
            "duplicate_semantic_adjacent": item.duplicate_semantic_adjacent,
            "entity_count": len(item.entity_rows),
            "series_count": len(item.series_rows),
        })

    entity_rows: List[Dict[str, Any]] = []
    for audit in file_audits:
        for entity_id, count in sorted(audit.entity_rows.items()):
            first = audit.entity_first.get(entity_id)
            last = audit.entity_last.get(entity_id)
            observed = len(audit.entity_minutes.get(entity_id, set()))
            span = int((last - first).total_seconds() // 60) + 1 if first and last else 0
            entity_rows.append({
                "city": audit.city,
                "source": audit.source,
                "entity_id": entity_id,
                "rows": count,
                "first_timestamp_utc": first.isoformat().replace("+00:00", "Z") if first else None,
                "last_timestamp_utc": last.isoformat().replace("+00:00", "Z") if last else None,
                "observed_minutes": observed,
                "span_minutes": span,
                "missing_minutes_internal": max(0, span - observed),
                "series_count": sum(1 for key in audit.series_rows if key == entity_id or key.startswith(entity_id + "|")),
            })

    field_rows: List[Dict[str, Any]] = []
    for audit in file_audits:
        for field_name, stats in audit.field_stats.items():
            field_rows.append({
                "city": audit.city,
                "source": audit.source,
                "field": field_name,
                "rows": stats.rows,
                "missing": stats.missing,
                "missing_ratio": round(stats.missing / stats.rows, 8) if stats.rows else None,
                "numeric": stats.numeric,
                "zero": stats.zero,
                "zero_ratio_nonmissing_numeric": round(stats.zero / stats.numeric, 8) if stats.numeric else None,
                "invalid_numeric": stats.invalid_numeric,
                "minimum": stats.minimum,
                "maximum": stats.maximum,
                "numeric_field_expected": field_is_numeric(audit.source, field_name),
            })

    gap_rows: List[Dict[str, Any]] = []
    for audit in file_audits:
        for series_key, missing in sorted(audit.series_gap_minutes.items(), key=lambda pair: (-pair[1], pair[0])):
            gap_rows.append({
                "city": audit.city,
                "source": audit.source,
                "series_key": series_key,
                "missing_minutes": missing,
                "gap_runs": audit.series_gap_runs[series_key],
                "max_single_gap_minutes": audit.series_max_gap[series_key],
            })

    counter_rows: List[Dict[str, Any]] = []
    for audit in file_audits:
        for (series_key, field_name), stats in sorted(audit.counter_stats.items()):
            counter_rows.append({
                "city": audit.city,
                "source": audit.source,
                "series_key": series_key,
                "field": field_name,
                **dict(stats),
            })

    registry_rows = []
    for item in sorted(entity_registry.values(), key=lambda x: (x["entity_type"], x["entity_id"])):
        registry_rows.append({
            "entity_type": item["entity_type"],
            "entity_id": item["entity_id"],
            "city": item["city"],
            "role": item["role"],
            "sources": "|".join(sorted(item["sources"])),
            "raw_aliases": "|".join(sorted(item["raw_aliases"])),
            "provenance": "|".join(sorted(item["provenance"])),
        })

    relation_rows = []
    for item in sorted(relations.values(), key=lambda x: (x["relation_type"], x["left_id"], x["right_id"])):
        relation_rows.append({
            "relation_type": item["relation_type"],
            "left_type": item["left_type"],
            "left_id": item["left_id"],
            "right_type": item["right_type"],
            "right_id": item["right_id"],
            "city": item["city"],
            "sources": "|".join(sorted(item["sources"])),
            "raw_fields": "|".join(sorted(item["raw_fields"])),
            "provenance": "|".join(sorted(item["provenance"])),
            "confidence": item["confidence"],
            "reason": item["reason"],
        })

    semantic_rows = []
    for contract in SEMANTIC_CONTRACTS:
        expected_field = contract["field"]
        matching = [
            row for row in field_rows
            if row["source"] == contract["source"]
            and (
                row["field"] == expected_field
                if not expected_field.startswith("*_")
                else row["field"].endswith(expected_field[1:])
            )
        ]
        observed_min = min((row["minimum"] for row in matching if row["minimum"] is not None), default=None)
        observed_max = max((row["maximum"] for row in matching if row["maximum"] is not None), default=None)
        missing_ratio = max((row["missing_ratio"] for row in matching if row["missing_ratio"] is not None), default=None)
        semantic_rows.append({
            "source": contract["source"],
            "field": contract["field"],
            "semantic_kind": contract["kind"],
            "declared_unit": contract["unit"],
            "direction": contract["direction"],
            "transform": contract["transform"],
            "evidence_role": contract["role"],
            "zero_semantics": contract["zero"],
            "missing_semantics": contract["missing"],
            "observed_min_across_files": observed_min,
            "observed_max_across_files": observed_max,
            "max_file_missing_ratio": missing_ratio,
            "semantic_status": "verified_from_schema_and_observed_values" if matching else "contract_only_or_not_observed",
            "review_note": "cpu/disk ratio versus percent must be reconciled before thresholding" if contract["field"] in {"cpu_usage", "disk_io_util"} else "",
        })

    target_city_by_service: Dict[Tuple[str, str], str] = {}
    for row in relation_rows:
        if row["relation_type"] == "targets_city":
            target_city_by_service[(row["city"], row["left_id"])] = row["right_id"]
    service_probe_rows = []
    for row in relation_rows:
        if row["relation_type"] != "observes_service":
            continue
        service_probe_rows.append({
            "source_city": row["city"],
            "observer_id": row["left_id"],
            "target_domain": row["right_id"],
            "target_city": target_city_by_service.get((row["city"], row["right_id"]), ""),
            "source": row["sources"],
            "raw_fields": row["raw_fields"],
            "provenance": row["provenance"],
            "confidence": row["confidence"],
            "reason": row["reason"],
        })

    source_summary: List[Dict[str, Any]] = []
    by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in quality_rows:
        by_source[row["source"]].append(row)
    for source, rows in sorted(by_source.items()):
        source_summary.append({
            "source": source,
            "files": len(rows),
            "cities": len({row["city"] for row in rows}),
            "rows": sum(int(row["rows"]) for row in rows),
            "storage": "|".join(sorted({row["storage"] for row in rows})),
            "first_timestamp_utc": min((row["first_timestamp_utc"] for row in rows if row["first_timestamp_utc"]), default=None),
            "last_timestamp_utc": max((row["last_timestamp_utc"] for row in rows if row["last_timestamp_utc"]), default=None),
            "missing_minutes_global_total": sum(int(row["missing_minutes_global"] or 0) for row in rows),
            "malformed_rows": sum(int(row["malformed_rows"]) for row in rows),
            "timestamp_parse_failures": sum(int(row["timestamp_parse_failures"]) for row in rows),
            "duplicate_exact_adjacent": sum(int(row["duplicate_exact_adjacent"]) for row in rows),
            "duplicate_semantic_adjacent": sum(int(row["duplicate_semantic_adjacent"]) for row in rows),
        })

    fieldnames = {
        "quality_by_city_source.csv": list(quality_rows[0].keys()) if quality_rows else [],
        "file_audit.json": [],
        "entity_coverage.csv": list(entity_rows[0].keys()) if entity_rows else [],
        "field_quality.csv": list(field_rows[0].keys()) if field_rows else [],
        "time_gaps.csv": list(gap_rows[0].keys()) if gap_rows else ["city", "source", "series_key", "missing_minutes", "gap_runs", "max_single_gap_minutes"],
        "counter_audit.csv": sorted({key for row in counter_rows for key in row}) if counter_rows else ["city", "source", "series_key", "field"],
        "entity_registry.csv": list(registry_rows[0].keys()) if registry_rows else [],
        "entity_relations.csv": list(relation_rows[0].keys()) if relation_rows else [],
        "service_probe_mapping.csv": list(service_probe_rows[0].keys()) if service_probe_rows else ["source_city", "observer_id", "target_domain", "target_city", "source", "raw_fields", "provenance", "confidence", "reason"],
        "metric_semantics.csv": list(semantic_rows[0].keys()) if semantic_rows else [],
        "source_summary.csv": list(source_summary[0].keys()) if source_summary else [],
    }
    write_csv(output_dir / "quality_by_city_source.csv", quality_rows, fieldnames["quality_by_city_source.csv"])
    write_csv(output_dir / "entity_coverage.csv", entity_rows, fieldnames["entity_coverage.csv"])
    write_csv(output_dir / "field_quality.csv", field_rows, fieldnames["field_quality.csv"])
    write_csv(output_dir / "time_gaps.csv", gap_rows, fieldnames["time_gaps.csv"])
    write_csv(output_dir / "counter_audit.csv", counter_rows, fieldnames["counter_audit.csv"])
    write_csv(output_dir / "entity_registry.csv", registry_rows, fieldnames["entity_registry.csv"])
    write_csv(output_dir / "entity_relations.csv", relation_rows, fieldnames["entity_relations.csv"])
    write_csv(output_dir / "service_probe_mapping.csv", service_probe_rows, fieldnames["service_probe_mapping.csv"])
    write_csv(output_dir / "metric_semantics.csv", semantic_rows, fieldnames["metric_semantics.csv"])
    write_csv(output_dir / "source_summary.csv", source_summary, fieldnames["source_summary.csv"])

    manifest = {
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "raw_root": str(root),
        "output_dir": str(output_dir),
        "expected_data_start_utc": DATA_START.isoformat().replace("+00:00", "Z"),
        "expected_data_end_exclusive_utc": DATA_END.isoformat().replace("+00:00", "Z"),
        "expected_minutes": EXPECTED_MINUTES,
        "include_netflow": include_netflow,
        "netflow_workers": (max(1, min(netflow_workers or min(4, max(1, os.cpu_count() or 1)), len(netflow_archives))) if netflow_archives else 0),
        "source_count": len(source_summary),
        "file_count": len(file_rows),
        "row_count": sum(int(row["rows"]) for row in file_rows),
        "city_count": len({row["city"] for row in quality_rows}),
        "limitations": [
            "duplicate counts are conservative adjacent-row counts; semantic duplicates are expected to be adjacent in the source order",
            "entity-level minute coverage is based on observed timestamp buckets and does not infer missing records outside first/last observation",
            "relations marked inferred/derived are not official physical topology",
            "no normal/fault labels are created",
        ],
        "files": file_rows,
        "source_summary": source_summary,
        "artifacts": [
            "quality_by_city_source.csv", "entity_coverage.csv", "field_quality.csv",
            "time_gaps.csv", "counter_audit.csv", "entity_registry.csv",
            "entity_relations.csv", "service_probe_mapping.csv", "metric_semantics.csv",
            "source_summary.csv", "audit_manifest.json", "audit_detail.json",
        ],
    }
    (output_dir / "audit_manifest.json").write_text(json.dumps(json_ready(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "audit_detail.json").write_text(json.dumps(json_ready({
        "files": [audit.finish() for audit in file_audits],
        "entities": registry_rows,
        "relations": relation_rows,
        "service_probe_mapping": service_probe_rows,
        "metric_semantics": semantic_rows,
    }), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elapsed = (datetime.now(UTC) - started).total_seconds()
    summary = {"output_dir": str(output_dir), "files": len(file_rows), "rows": manifest["row_count"], "sources": len(source_summary), "cities": manifest["city_count"], "elapsed_seconds": round(elapsed, 3)}
    print("[audit] complete", json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit AIOps raw data quality, entity relations and metric semantics")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--skip-netflow", action="store_true")
    parser.add_argument("--netflow-workers", type=int, default=4)
    args = parser.parse_args()
    run_audit(
        args.root.resolve(),
        args.output_dir.resolve(),
        include_netflow=not args.skip_netflow,
        netflow_workers=args.netflow_workers,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

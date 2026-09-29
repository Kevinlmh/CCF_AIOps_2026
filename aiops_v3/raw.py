"""Bounded-memory projection of the seven public CSV families.

The existing stage-one tensor cache is preferred on Mac. This builder is for
new raw batches and public samples; it never copies the source CSV files.
"""

from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import numpy as np

from .contracts import load_contract


NODE_FEATURES = [
    "node.cpu_usage", "node.load1", "node.load5", "node.memory_available_ratio",
    "node.swap_used_ratio", "node.disk_read_rate", "node.disk_write_rate",
    "node.disk_io_util", "node.filesystem_used_ratio", "node.inode_used_ratio",
    "node.open_fd_ratio", "node.process_count", "interface.carrier_changes",
    "interface.rx_drop_rate", "interface.tx_drop_rate", "interface.rx_error_rate",
    "interface.tx_error_rate", "routing.bgp_peer_up", "routing.bgp_command_success",
    "routing.bgp_peer_count", "routing.ipv6_route_exists", "routing.ipv6_route_count",
    "routing.ospf6_neighbor_state_code", "scrape.scrape_up", "netflow.bytes",
]
EDGE_FEATURES = [
    f"traffic.{flow}.{metric}"
    for flow in ("dns", "web", "auth", "elephant")
    for metric in ("error_ratio", "latency_p95_seconds", "loss_rate")
]
EDGE_FEATURES += [f"netflow.bytes.protocol_{protocol}" for protocol in (0, 6, 17, 58, 89, 255)]
LOG_FEATURES = ["frr.bgp.err.count", "frr.ospf.err.count", "frr.other.err.count"]
SOURCES = ("node", "interface", "routing", "scrape", "netflow", "frr", "traffic")


def _source(path: Path) -> str | None:
    name = path.name
    for prefix, source in (
        ("node_metrics", "node"), ("interface_metrics", "interface"),
        ("routing_metrics", "routing"), ("scrape_health", "scrape"),
        ("netflow_", "netflow"), ("frr_syslog_events", "frr"),
        ("traffic_flow_metrics", "traffic"),
    ):
        if name.startswith(prefix):
            return source
    return None


def _number(value: str | None) -> float | None:
    if value is None or value.strip() in {"", "NULL", "\\N", "nan", "NaN"}:
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if np.isfinite(result) else None


def _role(value: str | None) -> str | None:
    if not value:
        return None
    text = value.lower().replace("_", "-")
    for pattern in (r"service-vm-[123]", r"traffic-vm", r"monitor-vm", r"br-?[12]", r"cr-?[12]", r"(?<![a-z])fw(?![a-z])"):
        match = re.search(pattern, text)
        if match:
            role = match.group().replace("br1", "br-1").replace("br2", "br-2").replace("cr1", "cr-1").replace("cr2", "cr-2")
            return role
    return None


def _city(path: Path, cities: set[str]) -> str | None:
    for part in reversed(path.parts):
        city = part.split("_")[0]
        if city in cities:
            return city
    return None


def _window(path: Path) -> tuple[datetime, datetime]:
    for part in path.parts:
        match = re.search(r"(\d{14})_(\d{14})", part)
        if match:
            return tuple(datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc) for value in match.groups())
    raise ValueError(f"no time window in {path}")


def _minute(value: str | None, start: datetime, count: int) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    minute = int((dt.astimezone(timezone.utc) - start).total_seconds() // 60)
    return minute if 0 <= minute < count else None


def build_sample_store(raw_root: Path, destination: Path) -> Path:
    """Project raw CSVs into a small v3 store, with missing cells masked."""
    raw_root, destination = Path(raw_root), Path(destination)
    files = [
        (path, _source(path)) for path in sorted(raw_root.rglob("*.csv"))
        if path.parent.name == "processed" or path.parent.name.endswith("_data")
    ]
    files = [(path, source) for path, source in files if source]
    if not files:
        raise ValueError(f"no processed telemetry CSVs under {raw_root}")
    windows = {_window(path) for path, _ in files}
    start = min(item[0] for item in windows)
    end = max(item[1] for item in windows)
    minutes = int((end - start).total_seconds() // 60)
    if minutes <= 0:
        raise ValueError("empty time window")
    contract = load_contract()
    cities = {node.split("-", 1)[0] for node in contract.nodes}
    nodes = sorted(contract.nodes)
    node_index = {name: i for i, name in enumerate(nodes)}
    edges = [
        {"source": f"{source}-traffic-vm", "target": f"city:{target}", "relation": "traffic"}
        for source in sorted(cities) for target in sorted(cities)
    ]
    edges.extend(
        {"source": f"{city}-{role}", "target": f"interface:{city}-{role}:{interface}", "relation": "netflow"}
        for city in sorted(cities)
        for role in ("br-1", "br-2", "cr-1", "cr-2")
        for interface in ("ens4", "ens5")
    )
    edge_index = {(edge["source"], edge["target"]): i for i, edge in enumerate(edges)}
    values = {
        "node": np.zeros((minutes, len(nodes), len(NODE_FEATURES)), np.float32),
        "edge": np.zeros((minutes, len(edges), len(EDGE_FEATURES)), np.float32),
        "log": np.zeros((minutes, len(nodes), len(LOG_FEATURES)), np.float32),
    }
    masks = {kind: np.zeros(array.shape, bool) for kind, array in values.items()}
    features = {"node": NODE_FEATURES, "edge": EDGE_FEATURES, "log": LOG_FEATURES}
    feature_index = {kind: {name: i for i, name in enumerate(names)} for kind, names in features.items()}
    source_counts = dict.fromkeys(SOURCES, 0)
    accepted_rows = dict.fromkeys(SOURCES, 0)
    rejected_rows = dict.fromkeys(SOURCES, 0)
    rejected = defaultdict(int)
    counters: dict[tuple[str, str], float] = {}

    def set_cell(kind: str, t: int, entity: int, name: str, value: float, *, add: bool = False, minimum: bool = False) -> None:
        column = feature_index[kind].get(name)
        if column is None:
            return
        if add and masks[kind][t, entity, column]:
            values[kind][t, entity, column] += value
        elif minimum and masks[kind][t, entity, column]:
            values[kind][t, entity, column] = min(values[kind][t, entity, column], value)
        elif not masks[kind][t, entity, column] or value > values[kind][t, entity, column]:
            values[kind][t, entity, column] = value
        masks[kind][t, entity, column] = True

    for path, source in files:
        city = _city(path, cities)
        with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                source_counts[source] += 1
                if city is None:
                    rejected["unknown_city"] += 1
                    rejected_rows[source] += 1
                    continue
                time_field = {"netflow": "minute_utc", "frr": "event_time", "traffic": "timestamp_utc"}.get(source, "timestamp")
                t = _minute(row.get(time_field), start, minutes)
                if t is None:
                    rejected["invalid_or_outside_time"] += 1
                    rejected_rows[source] += 1
                    continue
                if source == "traffic":
                    source_city = (row.get("source_region") or "").lower()
                    target_city = (row.get("target_region") or "").lower()
                    edge = edge_index.get((f"{source_city}-traffic-vm", f"city:{target_city}"))
                    flow = (row.get("flow_type") or "").lower()
                    if edge is None or flow not in {"dns", "web", "auth", "elephant"}:
                        rejected["unmapped_traffic"] += 1
                        rejected_rows[source] += 1
                        continue
                    accepted_rows[source] += 1
                    identity = row.get("series_key") or f"{source_city}:{target_city}:{flow}"
                    deltas = {}
                    for field in ("requests_total", "error_total"):
                        current = _number(row.get(f"{flow}_flow_{field}"))
                        if current is None:
                            continue
                        key = (identity, field)
                        previous = counters.get(key)
                        counters[key] = current
                        if previous is not None:
                            deltas[field] = current - previous if current >= previous else current
                    requests = deltas.get("requests_total", 0)
                    if requests > 0 and "error_total" in deltas:
                        set_cell("edge", t, edge, f"traffic.{flow}.error_ratio", min(1.0, deltas["error_total"] / requests))
                    for field in ("latency_p95_seconds", "loss_rate"):
                        number = _number(row.get(f"{flow}_flow_{field}"))
                        if number is not None:
                            set_cell("edge", t, edge, f"traffic.{flow}.{field}", number)
                    continue
                role = _role(row.get("node") or row.get("node_key") or row.get("hostname"))
                node = node_index.get(f"{city}-{role}") if role else None
                if node is None:
                    rejected["unmapped_node"] += 1
                    rejected_rows[source] += 1
                    continue
                accepted_rows[source] += 1
                if source == "node":
                    for name in NODE_FEATURES:
                        if name.startswith("node."):
                            number = _number(row.get(name.split(".", 1)[1]))
                            if number is not None:
                                set_cell("node", t, node, name, number)
                elif source == "interface":
                    for name in NODE_FEATURES:
                        if name.startswith("interface."):
                            number = _number(row.get(name.split(".", 1)[1]))
                            if number is not None:
                                set_cell("node", t, node, name, number)
                elif source == "routing":
                    number = _number(row.get("value"))
                    if number is not None:
                        name = f"routing.{row.get('metric_name', '').lower()}"
                        set_cell("node", t, node, name, number, minimum=name in {
                            "routing.bgp_peer_up", "routing.bgp_command_success",
                            "routing.ipv6_route_exists", "routing.ospf6_neighbor_state_code",
                        })
                elif source == "scrape":
                    number = _number(row.get("scrape_up"))
                    if number is not None:
                        set_cell("node", t, node, "scrape.scrape_up", number, minimum=True)
                elif source == "netflow":
                    number = _number(row.get("bytes"))
                    if number is not None:
                        set_cell("node", t, node, "netflow.bytes", number, add=True)
                        interface = (row.get("interface_id") or "").lower()
                        protocol = (row.get("protocol") or "").strip()
                        edge = edge_index.get((f"{city}-{role}", f"interface:{city}-{role}:{interface}"))
                        if edge is not None and protocol in {"0", "6", "17", "58", "89", "255"}:
                            set_cell("edge", t, edge, f"netflow.bytes.protocol_{protocol}", number, add=True)
                elif source == "frr":
                    severity = (row.get("severity") or "").lower()
                    code = (row.get("severity_code") or "").strip()
                    if severity in {"err", "error", "critical", "warning"} or code in {"0", "1", "2", "3", "4"}:
                        message = (row.get("message") or "").lower()
                        family = "bgp" if "bgp" in message else "ospf" if "ospf" in message else "other"
                        set_cell("log", t, node, f"frr.{family}.err.count", 1, add=True)

    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for kind in ("node", "edge", "log"):
        np.save(destination / f"{kind}_values.npy", values[kind])
        np.save(destination / f"{kind}_mask.npy", masks[kind])
    manifest = {
        "format_version": 3, "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time": end.isoformat().replace("+00:00", "Z"), "minute_count": minutes,
        "entities": {"nodes": nodes, "edges": edges}, "features": features,
        "shapes": {kind: list(array.shape) for kind, array in values.items()},
        "source_counts": source_counts, "accepted_rows_by_source": accepted_rows,
        "rejected_rows_by_source": rejected_rows, "rejected_rows": dict(rejected),
        "source_files": [str(path) for path, _ in files],
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return destination

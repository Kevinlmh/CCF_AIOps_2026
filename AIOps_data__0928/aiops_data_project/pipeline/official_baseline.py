"""Streaming implementation of the public five-sigma event detector.

The public AIOps Challenge 2026 baseline uses this detector only to propose
time windows. Root-cause ranking and taxonomy classification happen later in
the baseline pipeline. This module mirrors the detector's observable rules
while adapting its input traversal to the supplied continuous city data:

* first 20% of each node/metric series as a global baseline;
* five-sigma deviations;
* 120-second event merge gap;
* exclude FRR syslog and high-cardinality netflow from the generic detector.

It is intentionally separate from the conservative evidence-aware detector in
``cases.py``. The two modes answer different questions: official compatibility
versus auditable data exploration.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .cases import normalize_role, parse_time
from .inventory import discover_region_data_dirs, source_from_filename


OFFICIAL_IGNORED_FIELDS = {
    "id", "port", "collector_port", "protocol", "src_port", "dst_port",
    "flow_record_count", "timestamp", "timestamp_utc",
    "prometheus_sample_time_utc", "minute_utc", "first_seen",
}


class RunningStats:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.total_sq = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.total_sq += value * value

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    @property
    def std(self) -> float:
        if self.count < 2:
            return 0.0
        return math.sqrt(max(0.0, self.total_sq / self.count - self.mean * self.mean))


def _number(value: str) -> Optional[float]:
    value = (value or "").strip().strip('"')
    if not value or value in {"NULL", "null", "\\N", "NA", "N/A", "nan", "NaN"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _node_id(row: Dict[str, str], city: str) -> Optional[str]:
    # This deliberately follows the public baseline: only explicit node or
    # node_key values create a candidate. Traffic-flow rows without a node
    # field are therefore not converted into synthetic traffic-vm candidates.
    raw = (row.get("node") or row.get("node_key") or "").strip()
    role = normalize_role(raw)
    return f"{city}-{role}" if role else None


def _timestamp(row: Dict[str, str]) -> Optional[datetime]:
    for field in ("timestamp", "timestamp_utc", "minute_utc", "first_seen"):
        value = row.get(field, "")
        parsed = parse_time(value)
        if parsed is not None:
            return parsed
    return None


def _numeric_fields(row: Dict[str, str]) -> Iterator[Tuple[str, float]]:
    # Same field-level exclusions as the public five_sigma detector.
    emitted = 0
    for key, raw in row.items():
        if key in OFFICIAL_IGNORED_FIELDS or key.endswith("_port"):
            continue
        value = _number(raw)
        if value is not None:
            yield key, value
            emitted += 1
            if emitted >= 32:
                return


def _rows(path: Path) -> Iterator[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        yield from csv.DictReader(handle)


def _series(path: Path, city: str, source_label: str, row: Dict[str, str]) -> Iterator[Tuple[datetime, str, float]]:
    timestamp = _timestamp(row)
    node = _node_id(row, city)
    if timestamp is None or node is None:
        return
    for metric, value in _numeric_fields(row):
        yield timestamp, f"{node}|{source_label}.{metric}", value


def _data_files(root: Path) -> Iterable[Tuple[str, str, Path]]:
    for city, data_dir in discover_region_data_dirs(root):
        for path in sorted(data_dir.glob("*.csv")):
            source = source_from_filename(path.name)
            if source is None or source in {"frr_syslog_events", "netflow_5tuple"}:
                continue
            # The baseline names a source by the first underscore token.
            source_label = path.stem.split("_", 1)[0]
            yield city, source_label, path


def _scan_file(path: Path, city: str, source_label: str, sigma: float) -> List[Dict[str, object]]:
    counts: Dict[str, int] = defaultdict(int)
    for row in _rows(path):
        for _timestamp_value, key, _value in _series(path, city, source_label, row):
            counts[key] += 1
    baseline_counts = {key: max(3, int(count * 0.2)) for key, count in counts.items() if count >= 4}
    stats: Dict[str, RunningStats] = {key: RunningStats() for key in baseline_counts}
    seen: Dict[str, int] = defaultdict(int)
    for row in _rows(path):
        for _timestamp_value, key, value in _series(path, city, source_label, row):
            if key in baseline_counts and seen[key] < baseline_counts[key]:
                stats[key].add(value)
                seen[key] += 1
    points: List[Dict[str, object]] = []
    seen_again: Dict[str, int] = defaultdict(int)
    for row in _rows(path):
        for timestamp, key, value in _series(path, city, source_label, row):
            if key not in baseline_counts:
                continue
            index = seen_again[key]
            seen_again[key] += 1
            if index < baseline_counts[key]:
                continue
            baseline = stats[key]
            deviation = abs(value - baseline.mean)
            if baseline.std > 0:
                is_anomaly = deviation > sigma * baseline.std
                magnitude = deviation / baseline.std
            else:
                is_anomaly = value != baseline.mean
                magnitude = deviation / 1e-12 if is_anomaly else 0.0
            if is_anomaly:
                node, metric = key.split("|", 1)
                points.append({
                    "time": timestamp,
                    "node": node,
                    "metric": metric,
                    "value": value,
                    "magnitude": magnitude,
                })
    return points


def detect_official_baseline_events(root: Path, sigma: float = 5.0, verbose: bool = True) -> List[Dict[str, object]]:
    points: List[Dict[str, object]] = []
    for city, source_label, path in _data_files(root):
        file_points = _scan_file(path, city, source_label, sigma)
        points.extend(file_points)
        if verbose:
            print(f"[official-baseline] {source_label} {city}: {len(file_points)} numeric anomalies")
    points.sort(key=lambda item: item["time"])
    events: List[Dict[str, object]] = []
    for point in points:
        timestamp = point["time"]
        if not events or timestamp - events[-1]["last_point_time"] > timedelta(seconds=120):
            events.append({"start": timestamp, "last_point_time": timestamp, "points": []})
        event = events[-1]
        event["last_point_time"] = max(event["last_point_time"], timestamp)
        event["points"].append(point)
    output = []
    for index, event in enumerate(events, 1):
        end = event["last_point_time"] + timedelta(minutes=1)
        output.append({
            "event_id": f"official_baseline_event_{index:04d}",
            "start_time": event["start"].isoformat().replace("+00:00", "Z"),
            "end_time": end.isoformat().replace("+00:00", "Z"),
            "duration_minutes": round((end - event["start"]).total_seconds() / 60.0, 3),
            "anomaly_count": len(event["points"]),
            "points": [
                {
                    **point,
                    "time": point["time"].isoformat().replace("+00:00", "Z"),
                    "magnitude": round(float(point["magnitude"]), 6),
                }
                for point in sorted(event["points"], key=lambda item: (-item["magnitude"], item["node"], item["metric"]))
            ],
            "note": "Official-baseline-compatible candidate window; RCA and classification are separate downstream stages.",
        })
    return output


def write_official_baseline_events(events: List[Dict[str, object]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    compact_events = []
    for event in events:
        points = list(event.get("points", []))
        compact_events.append({
            key: event[key]
            for key in ("event_id", "start_time", "end_time", "duration_minutes", "anomaly_count", "note")
            if key in event
        } | {
            "unique_nodes": sorted({str(point["node"]) for point in points}),
            "unique_metrics": sorted({str(point["metric"]) for point in points}),
            "top_points": points[:100],
            "top_points_note": "Only the top 100 points are retained; anomaly_count is the complete event count.",
        })
    (output_dir / "official_baseline_events.json").write_text(json.dumps(compact_events, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "official_baseline_events.jsonl").open("w", encoding="utf-8") as handle:
        for event in compact_events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

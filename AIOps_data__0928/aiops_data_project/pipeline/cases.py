"""Transparent unsupervised Case construction and evidence-based attribution.

The output is intentionally called *candidate cases*: the public raw package
does not contain root-cause or category labels. This module therefore produces
auditable hypotheses, not hidden ground truth.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .definitions import FAULT_TAXONOMY
from .inventory import discover_region_data_dirs, parse_time, source_from_filename


CITY_ALIASES = {
    "北大": "beida",
    "北京大学": "beida",
    "沈阳": "shenyang",
    "西安": "xian",
    "成都": "chengdu",
    "武汉": "wuhan",
    "上海": "shanghai",
    "南京": "nanjing",
    "广州": "guangzhou",
}


ROLE_PATTERNS = [
    (re.compile(r"service[-_]?vm[-_]?([123])", re.I), lambda m: "service-vm-" + m.group(1)),
    (re.compile(r"traffic[-_]?vm", re.I), lambda m: "traffic-vm"),
    (re.compile(r"monitor[-_]?vm", re.I), lambda m: "monitor-vm"),
    (re.compile(r"(?:^|[-_])br[-_]?([12])(?:$|[-_])", re.I), lambda m: "br-" + m.group(1)),
    (re.compile(r"(?:^|[-_])cr[-_]?([12])(?:$|[-_])", re.I), lambda m: "cr-" + m.group(1)),
    (re.compile(r"(?:^|[-_])(?:fw|firewall)(?:$|[-_])", re.I), lambda m: "fw"),
]


RESOURCE_FIELDS = {
    "cpu_usage", "load1", "load5", "memory_available_ratio", "swap_used_ratio",
    "disk_read_rate", "disk_write_rate", "disk_io_util", "filesystem_used_ratio",
    "inode_used_ratio", "open_fd_ratio", "process_count",
}
INTERFACE_FIELDS = {
    # Byte/packet rates describe workload intensity.  They are retained in
    # the schema inventory, but are deliberately not anomaly triggers here:
    # a traffic burst is not, by itself, a link fault.  Link attribution needs
    # operational error signals or carrier transitions.
    "rx_drop_rate", "tx_drop_rate", "rx_error_rate", "tx_error_rate",
    "carrier_changes",
}
TRAFFIC_GAUGE_SUFFIXES = (
    "_active", "_batch_concurrency", "_batch_duration_seconds", "_observed_qps",
    "_latency_mean_seconds", "_latency_p95_seconds", "_throughput_bps",
    "_loss_rate", "_jitter_seconds",
)
COUNTER_MARKERS = (
    "_total", "_count", "_sum", "_changed_total", "_uptime_seconds",
    "batches_started", "batches_completed", "batches_failed", "batches_timeout",
    "requests_total", "bytes_sent_total", "bytes_received_total",
)


@dataclass
class RunningStats:
    count: int = 0
    total: float = 0.0
    total_sq: float = 0.0

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
        variance = max(0.0, self.total_sq / self.count - self.mean * self.mean)
        return math.sqrt(variance)


def _number(value: str) -> Optional[float]:
    value = (value or "").strip().strip('"')
    if not value or value in {"NULL", "null", "\\N", "NA", "N/A", "nan", "NaN"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_city(value: str, fallback: str) -> str:
    raw = (value or "").strip().lower()
    if raw in {"beida", "shenyang", "xian", "chengdu", "wuhan", "shanghai", "nanjing", "guangzhou"}:
        return raw
    for chinese, english in CITY_ALIASES.items():
        if chinese in raw:
            return english
    if "ccf-aiops-" in raw:
        return raw.split("ccf-aiops-", 1)[1].split("-", 1)[0]
    return fallback


def normalize_role(raw_value: str) -> Optional[str]:
    raw = (raw_value or "").strip().lower()
    raw = raw.replace(" ", "")
    for pattern, converter in ROLE_PATTERNS:
        match = pattern.search(raw)
        if match:
            return converter(match)
    return None


def node_id_from_row(row: Dict[str, str], city: str, source: str) -> Optional[str]:
    if source == "traffic_flow_metrics":
        observation_city = normalize_city(row.get("source_region", ""), city)
        return observation_city + "-traffic-vm"
    raw = row.get("node") or row.get("hostname") or row.get("node_key") or ""
    role = normalize_role(raw)
    if role is None:
        return None
    return city + "-" + role


def _metric_is_counter(metric: str) -> bool:
    lower = metric.lower()
    return any(marker in lower for marker in COUNTER_MARKERS)


def _metric_values(source: str, row: Dict[str, str], city: str) -> Iterable[Tuple[str, float, str, Optional[str]]]:
    """Yield metric name, value, evidence family, and candidate node."""
    candidate = node_id_from_row(row, city, source)
    if source == "node_metrics":
        for field in RESOURCE_FIELDS:
            value = _number(row.get(field, ""))
            if value is not None and candidate:
                yield field, value, "resource", candidate
    elif source == "interface_metrics":
        for field in INTERFACE_FIELDS:
            value = _number(row.get(field, ""))
            if value is not None and candidate:
                yield field, value, "link", candidate
    elif source == "routing_metrics":
        metric_name = (row.get("metric_name") or "").strip()
        value = _number(row.get("value", ""))
        if metric_name and value is not None and candidate and not _metric_is_counter(metric_name):
            yield metric_name, value, "routing", candidate
    elif source == "traffic_flow_metrics":
        flow_type = (row.get("flow_type") or "").strip().lower()
        prefix = flow_type + "_flow_"
        if flow_type not in {"dns", "web", "auth", "elephant"}:
            return
        for field, raw in row.items():
            if not field.startswith(prefix) or _metric_is_counter(field):
                continue
            if not any(field.endswith(suffix) for suffix in TRAFFIC_GAUGE_SUFFIXES):
                continue
            value = _number(raw)
            if value is not None and candidate:
                yield field, value, "service", candidate
    elif source == "netflow_5tuple":
        for field in ("packets", "bytes"):
            value = _number(row.get(field, ""))
            if value is not None and candidate:
                yield field, value, "link", candidate


def _source_file_records(root: Path, include_netflow: bool) -> Iterable[Tuple[str, str, Path]]:
    for city, data_dir in discover_region_data_dirs(root):
        for path in sorted(data_dir.glob("*.csv")):
            source = source_from_filename(path.name)
            if source is None or source in {"frr_syslog_events", "scrape_health"}:
                continue
            if source == "netflow_5tuple" and not include_netflow:
                continue
            yield city, source, path


def _iter_rows(path: Path) -> Iterable[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        yield from csv.DictReader(handle)


def _timestamp_for(source: str, row: Dict[str, str]) -> Optional[datetime]:
    field = "timestamp_utc" if source == "traffic_flow_metrics" else "minute_utc" if source == "netflow_5tuple" else "timestamp"
    return parse_time(row.get(field, ""))


def _add_evidence(bucket_data: Dict[datetime, Dict[str, Dict[str, object]]], timestamp: datetime, candidate: str, source: str, category: str, metric: str, magnitude: float, value: Optional[float] = None, note: str = "") -> None:
    bucket = timestamp.replace(second=0, microsecond=0)
    candidate_data = bucket_data.setdefault(bucket, {}).setdefault(candidate, {
        "score": 0.0,
        "count": 0,
        "sources": set(),
        "categories": Counter(),
        "metrics": Counter(),
        "first_seen": bucket,
        "evidence": [],
    })
    source_weight = 1.0
    if source == "frr_syslog_events":
        source_weight = 1.25
    elif source == "routing_metrics":
        source_weight = 1.1
    elif source == "traffic_flow_metrics":
        source_weight = 0.9
    elif source == "scrape_health":
        source_weight = 0.35
    weighted = min(50.0, max(0.0, magnitude)) * source_weight
    candidate_data["score"] = float(candidate_data["score"]) + weighted
    candidate_data["count"] = int(candidate_data["count"]) + 1
    candidate_data["sources"].add(source)
    candidate_data["categories"][category] += weighted
    candidate_data["metrics"][f"{source}.{metric}"] += weighted
    candidate_data["first_seen"] = min(candidate_data["first_seen"], bucket)
    evidence = {
        "timestamp_utc": bucket.isoformat().replace("+00:00", "Z"),
        "source": source,
        "category": category,
        "metric": metric,
        "magnitude": round(float(magnitude), 4),
        "value": round(float(value), 6) if value is not None else None,
        "note": note,
    }
    candidate_data["evidence"].append(evidence)
    candidate_data["evidence"] = sorted(candidate_data["evidence"], key=lambda item: -item["magnitude"])[:12]


def _scan_numeric_file(path: Path, city: str, source: str, sigma: float, baseline_fraction: float, bucket_data: Dict[datetime, Dict[str, Dict[str, object]]], verbose: bool) -> None:
    first: Optional[datetime] = None
    last: Optional[datetime] = None
    for row in _iter_rows(path):
        timestamp = _timestamp_for(source, row)
        if timestamp is None:
            continue
        first = timestamp if first is None or timestamp < first else first
        last = timestamp if last is None or timestamp > last else last
    if first is None or last is None or last <= first:
        return
    baseline_end = first + (last - first) * baseline_fraction
    # Use a minute-of-day seasonal baseline. The observations contain strong
    # daily operating cycles; a single global mean would systematically turn
    # every daily traffic peak into a false incident.
    stats: Dict[Tuple[str, str, int], RunningStats] = {}
    for row in _iter_rows(path):
        timestamp = _timestamp_for(source, row)
        if timestamp is None or timestamp > baseline_end:
            continue
        candidate = node_id_from_row(row, city, source)
        for metric, value, _category, yielded_candidate in _metric_values(source, row, city):
            if yielded_candidate != candidate or not candidate:
                continue
            minute_of_day = timestamp.hour * 60 + timestamp.minute
            stats.setdefault((candidate, metric, minute_of_day), RunningStats()).add(value)
    anomalies = 0
    # A persistent level shift is one diagnostic episode, not one new Case per
    # minute. Keep only the start of each anomaly run for each series. This is
    # especially important for near-constant ratios such as filesystem usage.
    last_anomaly_time: Dict[Tuple[str, str], datetime] = {}
    for row in _iter_rows(path):
        timestamp = _timestamp_for(source, row)
        if timestamp is None or timestamp <= baseline_end:
            continue
        for metric, value, category, candidate in _metric_values(source, row, city):
            if not candidate:
                continue
            minute_of_day = timestamp.hour * 60 + timestamp.minute
            baseline = stats.get((candidate, metric, minute_of_day))
            if baseline is None or baseline.count < 10:
                continue
            deviation = abs(value - baseline.mean)
            # The standard deviation of deterministic exporter values can be
            # nearly zero. A relative scale floor prevents tiny rounding or
            # exporter quantization changes from becoming astronomical scores.
            relative_floor = max(abs(baseline.mean) * 0.05, 1e-4)
            scale = max(baseline.std, relative_floor)
            magnitude = deviation / scale
            if magnitude > sigma:
                anomalies += 1
                series_key = (candidate, metric)
                previous = last_anomaly_time.get(series_key)
                if previous is None or timestamp - previous > timedelta(minutes=5):
                    _add_evidence(
                        bucket_data,
                        timestamp,
                        candidate,
                        source,
                        category,
                        metric,
                        min(50.0, magnitude),
                        value=value,
                        note=f"baseline_mean={baseline.mean:.6g};baseline_std={baseline.std:.6g};scale={scale:.6g}",
                    )
                last_anomaly_time[series_key] = timestamp
    if verbose:
        print(f"[cases] {source} {city}: {anomalies} numeric anomalies")


def _scan_syslog(path: Path, city: str, bucket_data: Dict[datetime, Dict[str, Dict[str, object]]]) -> None:
    severity_weight = {"err": 3.0, "warning": 2.0, "info": 0.5, "debug": 0.2}
    seen = set()
    for row in _iter_rows(path):
        severity = (row.get("severity") or "").strip().lower()
        if severity not in {"err", "warning"}:
            continue
        timestamp = parse_time(row.get("event_time", ""))
        if timestamp is None:
            continue
        candidate = node_id_from_row(row, city, "frr_syslog_events")
        if not candidate:
            continue
        program = (row.get("program") or "frr").strip()
        message = (row.get("message") or "").strip().replace("\n", " ")[:180]
        # Exact duplicate collector events at the same timestamp do not add
        # independent evidence.
        dedup_key = (timestamp, candidate, program, message)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        _add_evidence(
            bucket_data,
            timestamp,
            candidate,
            "frr_syslog_events",
            "routing",
            program,
            severity_weight.get(severity, 1.0),
            note=f"{severity}: {message}",
        )


def _scan_scrape_health(path: Path, city: str, bucket_data: Dict[datetime, Dict[str, Dict[str, object]]]) -> None:
    for row in _iter_rows(path):
        if (row.get("scrape_up") or "").strip() != "0":
            continue
        timestamp = parse_time(row.get("timestamp", ""))
        if timestamp is None:
            continue
        candidate = node_id_from_row(row, city, "scrape_health")
        if candidate:
            _add_evidence(
                bucket_data,
                timestamp,
                candidate,
                "scrape_health",
                "observability",
                row.get("exporter_type", "scrape_up"),
                1.0,
                note="scrape_up=0; treat as observability evidence, not direct root cause",
            )


def _serialize_bucket_data(bucket_data: Dict[datetime, Dict[str, Dict[str, object]]]) -> List[Tuple[datetime, Dict[str, Dict[str, object]]]]:
    return sorted(bucket_data.items(), key=lambda item: item[0])


def _event_records(bucket_data: Dict[datetime, Dict[str, Dict[str, object]]], merge_gap_minutes: int, min_bucket_points: int) -> List[List[Tuple[datetime, Dict[str, Dict[str, object]]]]]:
    active = []
    for timestamp, candidates in _serialize_bucket_data(bucket_data):
        points = sum(int(item["count"]) for item in candidates.values())
        diagnostic_points = sum(int(item["count"]) for item in candidates.values() if "observability" not in item["categories"])
        sources = set().union(*(set(item["sources"]) for item in candidates.values())) if candidates else set()
        # An isolated FRR warning or scrape failure is not enough to define a
        # Case. It becomes useful when corroborated by numeric evidence in the
        # same merged episode.
        only_observability = sources and sources <= {"scrape_health"}
        only_frr = sources and sources <= {"frr_syslog_events"}
        if points >= min_bucket_points and diagnostic_points > 0 and not only_observability and not only_frr:
            active.append((timestamp, candidates))
    events: List[List[Tuple[datetime, Dict[str, Dict[str, object]]]]] = []
    for item in active:
        if not events or item[0] - events[-1][-1][0] > timedelta(minutes=merge_gap_minutes):
            events.append([item])
        else:
            events[-1].append(item)
    return events


def _candidate_summary(event: List[Tuple[datetime, Dict[str, Dict[str, object]]]]) -> Dict[str, Dict[str, object]]:
    start = event[0][0]
    summary: Dict[str, Dict[str, object]] = {}
    for timestamp, candidates in event:
        for candidate, data in candidates.items():
            item = summary.setdefault(candidate, {
                "score": 0.0,
                "count": 0,
                "sources": set(),
                "categories": Counter(),
                "metrics": Counter(),
                "first_seen": timestamp,
                "evidence": [],
            })
            item["score"] += float(data["score"])
            item["count"] += int(data["count"])
            item["sources"].update(data["sources"])
            item["categories"].update(data["categories"])
            item["metrics"].update(data["metrics"])
            item["first_seen"] = min(item["first_seen"], data["first_seen"])
            item["evidence"].extend(data["evidence"])
    for candidate, item in summary.items():
        item["evidence"] = sorted(item["evidence"], key=lambda value: -value["magnitude"])[:20]
        offset_minutes = max(0.0, (item["first_seen"] - start).total_seconds() / 60.0)
        item["root_score"] = float(item["score"]) * (1.0 + 0.15 * max(0, len(item["sources"]) - 1)) + 2.0 / (1.0 + offset_minutes)
    return summary


def _category_and_subcategory(summary: Dict[str, Dict[str, object]], event: List[Tuple[datetime, Dict[str, Dict[str, object]]]]) -> Tuple[str, str, Dict[str, float], str]:
    category_scores: Counter = Counter()
    metric_names: Counter = Counter()
    for item in summary.values():
        for category, score in item["categories"].items():
            if category in FAULT_TAXONOMY:
                category_scores[category] += float(score)
        metric_names.update(item["metrics"])
    if not category_scores:
        return "unknown", "unknown", {}, "没有足够的诊断类证据。"
    major = category_scores.most_common(1)[0][0]
    lower_metrics = " ".join(metric_names.keys()).lower()
    if major == "resource":
        if "cpu" in lower_metrics or "load" in lower_metrics:
            sub = "cpu_pressure"
        elif "memory" in lower_metrics or "swap" in lower_metrics:
            sub = "memory_pressure"
        elif "disk_io" in lower_metrics or "disk_read" in lower_metrics or "disk_write" in lower_metrics:
            sub = "disk_io_pressure"
        elif "filesystem" in lower_metrics or "inode" in lower_metrics:
            sub = "disk_space_low"
        elif "process" in lower_metrics:
            sub = "process_pressure"
        else:
            sub = "softirq_pressure"
    elif major == "routing":
        if "ospf6_neighbor" in lower_metrics:
            sub = "ospf6_neighbor_down"
        elif "bgp_peer_up" in lower_metrics or "bgp_command_success" in lower_metrics:
            sub = "bgp_session_down"
        elif "bgp_peer_prefix" in lower_metrics or "route_change" in lower_metrics:
            sub = "bgp_route_flap"
        elif "default_route" in lower_metrics:
            sub = "wrong_default_route"
        else:
            sub = "blackhole"
    elif major == "link":
        if "drop" in lower_metrics or "error" in lower_metrics or "loss" in lower_metrics:
            sub = "loss"
        elif "latency" in lower_metrics or "jitter" in lower_metrics:
            sub = "delay"
        else:
            sub = "rate_limit"
    elif major == "service":
        if "dns" in lower_metrics:
            sub = "dns_down"
        elif "web" in lower_metrics and ("latency" in lower_metrics or "duration" in lower_metrics):
            sub = "web_slow"
        elif "web" in lower_metrics:
            sub = "web_5xx"
        elif "auth" in lower_metrics and ("timeout" in lower_metrics or "duration" in lower_metrics):
            sub = "auth_timeout"
        else:
            sub = "auth_error"
    else:
        if "drop" in lower_metrics or "error" in lower_metrics:
            sub = "acl_drop"
        elif "cpu" in lower_metrics:
            sub = "cpu_pressure"
        else:
            sub = "rate_limit"
    evidence_summary = []
    for item in summary.values():
        for evidence in item["evidence"]:
            metric = evidence.get("metric", "")
            value = evidence.get("value")
            if metric and value is not None:
                evidence_summary.append(f"{metric}={value:g}")
    evidence_summary = list(dict.fromkeys(evidence_summary))[:4]
    if major == "routing" and sub == "bgp_session_down":
        explanation = (
            f"{major}/{sub}：检测到 BGP 会话状态证据（{', '.join(evidence_summary) or 'bgp_peer_up 异常'}），"
            "其中 peer_up 下降/为 0 且伴随 prefix_received 异常；该模式比普通业务流量波动更直接地指向控制面会话中断。"
        )
    else:
        explanation = f"{major}/{sub} 得分最高；关键证据为 {', '.join(evidence_summary) or ', '.join(sorted(metric_names)[:4])}。"
    return major, sub, {key: round(float(value), 4) for key, value in category_scores.items()}, explanation


def _finalize_event(index: int, event: List[Tuple[datetime, Dict[str, Dict[str, object]]]]) -> Dict[str, object]:
    start = event[0][0]
    end = event[-1][0] + timedelta(minutes=1)
    summary = _candidate_summary(event)
    ordered = sorted(summary.items(), key=lambda item: (-float(item[1]["root_score"]), item[0]))
    top5 = []
    for rank, (candidate, data) in enumerate(ordered[:5], 1):
        top5.append({
            "rank": rank,
            "network_element_id": candidate,
            "root_score": round(float(data["root_score"]), 4),
            "anomaly_count": int(data["count"]),
            "sources": sorted(data["sources"]),
            "first_evidence_time": data["first_seen"].isoformat().replace("+00:00", "Z"),
            "evidence": data["evidence"],
        })
    major, sub, category_scores, category_reason = _category_and_subcategory(summary, event)
    root_reason = "未形成明确根因候选。"
    if top5:
        root = top5[0]
        key_evidence = []
        for evidence in root.get("evidence", [])[:4]:
            metric = evidence.get("metric", "")
            value = evidence.get("value")
            if metric and value is not None:
                key_evidence.append(f"{metric}={value:g}")
        evidence_clause = f"关键字段 {', '.join(dict.fromkeys(key_evidence))}；" if key_evidence else ""
        root_reason = (
            f"首位候选 {root['network_element_id']} 在 {root['first_evidence_time']} 首先出现证据，"
            f"累计 {root['anomaly_count']} 个异常点，覆盖 {', '.join(root['sources'])}；"
            f"{evidence_clause}该排序是基于异常强度、时间先后和多源支持的可解释启发式结果。"
        )
    # Do not pad the list with topology roles that have zero evidence.  A
    # shorter list is an explicit uncertainty signal; fabricating zero-score
    # candidates would make the report look more complete while weakening the
    # audit trail needed for root-cause review.
    return {
        "case_id": f"candidate_case_{index:04d}",
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time": end.isoformat().replace("+00:00", "Z"),
        "duration_minutes": round((end - start).total_seconds() / 60.0, 3),
        "bucket_count": len(event),
        "root_cause_top5": top5,
        "fault_category": {"major_category": major, "sub_category": sub},
        "root_cause_reason": root_reason,
        "classification_reason": category_reason,
        "category_scores": category_scores,
        "limitations": [
            "这是无监督候选 Case，不是官方 ground truth。",
            "netflow 未展开时，流量证据不完整。",
            "公开拓扑是参考拓扑，尚未替代真实链路拓扑。",
        ],
    }


def detect_candidate_cases(root: Path, sigma: float = 5.0, baseline_fraction: float = 0.2, merge_gap_minutes: int = 5, min_bucket_points: int = 2, include_netflow: bool = False, verbose: bool = True) -> List[Dict[str, object]]:
    bucket_data: Dict[datetime, Dict[str, Dict[str, object]]] = {}
    for city, data_dir in discover_region_data_dirs(root):
        for path in sorted(data_dir.glob("*.csv")):
            source = source_from_filename(path.name)
            if source == "frr_syslog_events":
                _scan_syslog(path, city, bucket_data)
            elif source == "scrape_health":
                _scan_scrape_health(path, city, bucket_data)
            elif source in {"node_metrics", "interface_metrics", "routing_metrics", "traffic_flow_metrics", "netflow_5tuple"}:
                if source == "netflow_5tuple" and not include_netflow:
                    continue
                _scan_numeric_file(path, city, source, sigma, baseline_fraction, bucket_data, verbose)
    events = _event_records(bucket_data, merge_gap_minutes, min_bucket_points)
    return [_finalize_event(index, event) for index, event in enumerate(events, 1)]


def write_cases(cases: List[Dict[str, object]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "candidate_cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "candidate_cases.jsonl").open("w", encoding="utf-8") as handle:
        for item in cases:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    rows = []
    for item in cases:
        root = item.get("root_cause_top5", [{}])[0]
        category = item.get("fault_category", {})
        rows.append({
            "case_id": item["case_id"],
            "start_time": item["start_time"],
            "end_time": item["end_time"],
            "duration_minutes": item["duration_minutes"],
            "root_cause_rank1": root.get("network_element_id", ""),
            "fault_major": category.get("major_category", ""),
            "fault_sub": category.get("sub_category", ""),
            "root_cause_reason": item["root_cause_reason"],
            "classification_reason": item["classification_reason"],
        })
    with (output_dir / "candidate_cases.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = list(rows[0].keys()) if rows else ["case_id"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

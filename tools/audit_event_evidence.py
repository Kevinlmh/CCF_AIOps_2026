"""Audit which evidence actually drove each detected event.

Two evidence patterns were found to be structurally unreliable on the formal
data and both can open an event on their own:

1. Service ratios (``traffic.<flow>.success_ratio`` / ``error_ratio``) are
   differences of cumulative counters.  Over a one-minute window with only a
   handful of requests, the ratio can swing to an extreme on pure sampling
   noise, yet the detector scores it against a constant baseline with a 0.02
   floor and saturates at the maximum.
2. ``node.disk_read_rate`` / ``node.disk_write_rate`` alternate between 0 and
   their working level as part of normal burstiness.  The drop to zero is
   scored as a ``low`` deviation.

This script replays the raw rows behind every trigger point so the counts are
computed from the source data rather than assumed.

Usage::

    python tools/audit_event_evidence.py \
        --inference-log outputs/v1_3/chengdu/final/chengdu_v1_3_inference.json \
        --data-root data/stage1/regions/chengdu_.../chengdu_..._data \
        --report outputs/v1_3/chengdu/evidence_audit.json
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
from pathlib import Path
from typing import Any

RATIO_SUFFIXES = {"success_ratio", "error_ratio"}
DISK_RATE_METRICS = {"node.disk_read_rate", "node.disk_write_rate"}

# A ratio built from fewer than this many requests in the window is treated as
# an unstable sample rather than a measured service degradation.
TINY_SAMPLE_REQUESTS = 30

# A disk rate series that reaches exactly zero in at least this share of its
# samples is treated as normally bursty rather than as a stopped device.
PERIODIC_ZERO_FRACTION = 0.05


def _number(value: str | None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    import math

    return number if math.isfinite(number) else None


def _city_of(path: Path, cities: list[str]) -> str:
    for part in path.parts:
        for city in cities:
            if part.startswith(city + "_") or part == city:
                return city
    return ""


def _traffic_rows(data_root: Path) -> dict[str, list[dict[str, str]]]:
    """Index traffic rows by series_key, in file order (the parser's order).

    Recurses so a whole ``regions/`` tree works as well as one city directory.
    ``series_key`` is a hash over region/domain/protocol and does not repeat
    across cities.
    """
    series: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for path in sorted(data_root.rglob("traffic_flow_metrics*.csv")):
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            for row in csv.DictReader(handle):
                key = (row.get("series_key") or "").strip()
                if key:
                    series[key].append(row)
    return series


def _ratio_sample_size(
    rows: list[dict[str, str]],
    timestamp: str,
    flow_type: str,
    kind: str,
    *,
    long_window_minutes: int = 30,
) -> dict[str, Any] | None:
    """Recompute the request delta behind one ratio evidence point.

    Also recomputes the same ratio over a trailing ``long_window_minutes``
    window.  If the one-minute ratio is extreme while the longer window is
    normal, the event was opened by sampling noise rather than by a real
    service degradation.
    """
    request_field = f"{flow_type}_flow_requests_total"
    numerator_field = f"{flow_type}_flow_{kind}_total"
    stamps = [row.get("timestamp_utc", "").strip() for row in rows]
    try:
        index = stamps.index(timestamp)
    except ValueError:
        return None
    if index == 0:
        return None

    def delta(start: int, end: int) -> tuple[float, float] | None:
        old_requests = _number(rows[start].get(request_field))
        new_requests = _number(rows[end].get(request_field))
        old_numerator = _number(rows[start].get(numerator_field))
        new_numerator = _number(rows[end].get(numerator_field))
        if None in (old_requests, new_requests, old_numerator, new_numerator):
            return None
        if new_requests < old_requests:  # counter reset
            return new_requests, new_numerator
        return new_requests - old_requests, new_numerator - old_numerator

    short = delta(index - 1, index)
    if short is None:
        return None

    # Walk back until the window is wide enough in wall-clock terms.
    anchor = index - 1
    for candidate in range(index - 2, -1, -1):
        anchor = candidate
        older = stamps[candidate][:16]
        newer = stamps[index - 1][:16]
        try:
            from datetime import datetime

            span = (
                datetime.fromisoformat(newer) - datetime.fromisoformat(older)
            ).total_seconds() / 60.0
        except ValueError:
            break
        if span >= long_window_minutes:
            break
    long = delta(anchor, index)

    return {
        "requests_in_window": short[0],
        "numerator_in_window": short[1],
        "ratio_in_window": short[1] / short[0] if short[0] else None,
        "long_window_minutes": long_window_minutes,
        "long_window_requests": long[0] if long else None,
        "long_window_ratio": (long[1] / long[0]) if long and long[0] else None,
    }


def _disk_series(data_root: Path, metric: str, cities: list[str]) -> dict[str, list[float]]:
    """Load one disk-rate metric keyed by public element id (``<city>-<role>``).

    The raw ``node`` column holds a bare role such as ``br-1`` which repeats in
    every city, so the city has to come from the path.
    """
    field = metric.split(".", 1)[1]
    result: dict[str, list[float]] = collections.defaultdict(list)
    for path in sorted(data_root.rglob("node_metrics_*.csv")):
        city = _city_of(path, cities)
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            for row in csv.DictReader(handle):
                value = _number(row.get(field))
                if value is None:
                    continue
                node = (row.get("node") or "").strip()
                result[f"{city}-{node}" if city else node].append(value)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inference-log", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from aiops_challenge_2026.config import load_public_config

    cities = list(load_public_config("network_elements")["cities"])
    log = json.loads(args.inference_log.read_text(encoding="utf-8"))
    events = log.get("events", [])
    traffic = _traffic_rows(args.data_root)
    disk_cache: dict[str, dict[str, list[float]]] = {}

    records = []
    for event in events:
        # Evidence points carry no node id of their own; it lives on the
        # candidate.  Fold it in so the raw series can be located again.
        points = [
            {**point, "node_id": candidate.get("node_id")}
            for candidate in event.get("candidates", [])
            for point in candidate.get("evidence", [])
        ]
        triggers = [point for point in points if point.get("event_role") == "trigger"]
        driver = max(triggers, key=lambda item: item["score"]) if triggers else None

        record: dict[str, Any] = {
            "prediction_id": event["prediction_id"],
            "start_time": event["start_time"],
            "trigger_count": len(triggers),
            "driver_metric": driver["metric"] if driver else None,
            "flags": [],
        }

        for point in points:
            is_driver = point is driver
            parts = point["metric"].split(".")
            if point["source"] == "traffic" and len(parts) == 3 and parts[2] in RATIO_SUFFIXES:
                kind = parts[2].split("_")[0]
                rows = traffic.get(dict(point.get("dimensions", {})).get("series_key", ""), [])
                info = _ratio_sample_size(
                    rows, point["timestamp_utc"].rstrip("Z").replace("T", " "), parts[1], kind
                )
                if info and info["requests_in_window"] < TINY_SAMPLE_REQUESTS:
                    short_ratio = info["ratio_in_window"]
                    long_ratio = info["long_window_ratio"]
                    normal_high = kind == "success"  # success should be near 1
                    contradicted = False
                    if short_ratio is not None and long_ratio is not None:
                        extreme = (
                            short_ratio < 0.9 if normal_high else short_ratio > 0.1
                        )
                        normal = (
                            long_ratio >= 0.95 if normal_high else long_ratio <= 0.05
                        )
                        contradicted = extreme and normal
                    record["flags"].append(
                        {
                            "kind": "tiny_sample_ratio",
                            "metric": point["metric"],
                            "event_role": point.get("event_role"),
                            "is_driver": is_driver,
                            "requests_in_window": info["requests_in_window"],
                            "ratio_in_window": (
                                round(short_ratio, 4) if short_ratio is not None else None
                            ),
                            "long_window_requests": info["long_window_requests"],
                            "long_window_ratio": (
                                round(long_ratio, 4) if long_ratio is not None else None
                            ),
                            "contradicted_by_long_window": contradicted,
                        }
                    )
            elif point["metric"] in DISK_RATE_METRICS and point["value"] == 0:
                metric = point["metric"]
                if metric not in disk_cache:
                    disk_cache[metric] = _disk_series(args.data_root, metric, cities)
                values = disk_cache[metric].get(point.get("node_id") or "", [])
                if values:
                    zero_fraction = sum(1 for value in values if value == 0) / len(values)
                    if zero_fraction >= PERIODIC_ZERO_FRACTION:
                        record["flags"].append(
                            {
                                "kind": "periodic_zero_disk_rate",
                                "metric": metric,
                                "event_role": point.get("event_role"),
                                "is_driver": is_driver,
                                "zero_fraction": round(zero_fraction, 4),
                            }
                        )
        records.append(record)

    # ---- aggregate ----
    def flagged(kind: str, *, driver_only: bool) -> int:
        return sum(
            1
            for record in records
            if any(
                flag["kind"] == kind and (flag["is_driver"] or not driver_only)
                for flag in record["flags"]
            )
        )

    total = len(records)
    tiny_driver = flagged("tiny_sample_ratio", driver_only=True)
    tiny_any = flagged("tiny_sample_ratio", driver_only=False)
    zero_driver = flagged("periodic_zero_disk_rate", driver_only=True)
    zero_any = flagged("periodic_zero_disk_rate", driver_only=False)
    either_driver = sum(
        1
        for record in records
        if any(flag["is_driver"] for flag in record["flags"])
    )

    request_counts = sorted(
        flag["requests_in_window"]
        for record in records
        for flag in record["flags"]
        if flag["kind"] == "tiny_sample_ratio" and flag["is_driver"]
    )
    driver_metrics = collections.Counter(
        record["driver_metric"]
        for record in records
        if any(
            flag["kind"] == "tiny_sample_ratio" and flag["is_driver"]
            for flag in record["flags"]
        )
    )

    contradicted = sum(
        1
        for record in records
        if any(
            flag["kind"] == "tiny_sample_ratio"
            and flag["is_driver"]
            and flag.get("contradicted_by_long_window")
            for flag in record["flags"]
        )
    )
    summary = {
        "events": total,
        "tiny_sample_ratio_as_driver": tiny_driver,
        "tiny_sample_ratio_as_driver_contradicted": contradicted,
        "tiny_sample_ratio_present": tiny_any,
        "periodic_zero_disk_rate_as_driver": zero_driver,
        "periodic_zero_disk_rate_present": zero_any,
        "either_pattern_as_driver": either_driver,
        "either_pattern_as_driver_ratio": round(either_driver / total, 4) if total else 0.0,
        "driver_tiny_sample_request_counts": request_counts,
        "driver_tiny_sample_metrics": dict(driver_metrics.most_common()),
        "thresholds": {
            "tiny_sample_requests": TINY_SAMPLE_REQUESTS,
            "periodic_zero_fraction": PERIODIC_ZERO_FRACTION,
        },
    }

    print(f"事件总数: {total}")
    print()
    print("【问题 A】服务质量 ratio 由极小样本算出（trigger 角色）")
    print(f"  作为触发指标(开事件的那条): {tiny_driver}  ({tiny_driver/total:.1%})")
    print(f"    其中被 30 分钟窗口推翻  : {contradicted}  "
          f"({contradicted/total:.1%} of all events)")
    print(f"  事件中至少含一条            : {tiny_any}  ({tiny_any/total:.1%})")
    if request_counts:
        print(f"  驱动点的窗口内请求数分布     : 中位={request_counts[len(request_counts)//2]}"
              f"  最小={request_counts[0]}  最大={request_counts[-1]}")
    print("  驱动指标构成:")
    for metric, count in driver_metrics.most_common():
        print(f"      {metric:<40s}{count:4d}")
    print()
    print("【问题 B】磁盘速率周期性归零被当成异常（support 角色，开不了事件）")
    print(f"  作为触发指标: {zero_driver}  ({zero_driver/total:.1%})")
    print(f"  事件中至少含一条: {zero_any}  ({zero_any/total:.1%})  ← 只污染 RCA 与分类")
    print()
    print(f"【合计】被问题 A **开出来**的事件: {either_driver} / {total} = {either_driver/total:.1%}")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps({"summary": summary, "events": records}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\n详细报告: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

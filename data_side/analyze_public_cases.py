"""Independently derive the three public-case resource evidence tables."""

from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path
from statistics import median

from aiops_challenge_2026.schema import load_jsonl, validate_ground_truth
from baseline.bian.preprocessing.observations import parse_time


BASE = Path(__file__).resolve().parent
REPO = BASE.parent
FIELDS = {
    "cpu_pressure": ("cpu_usage", "load1", "load5", "memory_available_ratio"),
    "memory_pressure": ("memory_available_ratio", "swap_used_ratio", "cpu_usage"),
    "disk_io_pressure": ("disk_io_util", "disk_read_rate", "disk_write_rate", "cpu_usage"),
}
PRIMARY = {
    "cpu_pressure": ("cpu_usage", "high"),
    "memory_pressure": ("memory_available_ratio", "low"),
    "disk_io_pressure": ("disk_io_util", "high"),
}


def observed_nodes(case_dir: Path, city: str) -> dict[str, list[dict]]:
    files = list(case_dir.glob(f"**/{city}_*/processed/node_metrics*.csv"))
    if len(files) != 1:
        raise ValueError(f"expected one {city} node CSV in {case_dir}, found {len(files)}")
    result: dict[str, list[dict]] = defaultdict(list)
    with files[0].open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            timestamp = parse_time(row.get("timestamp"))
            if timestamp is None:
                continue
            result[row["node"]].append({**row, "timestamp": timestamp})
    return result


def numbers(rows: list[dict], field: str, start: datetime | None, end: datetime | None) -> list[float]:
    values = []
    for row in rows:
        stamp = row["timestamp"]
        if start is not None and stamp < start:
            continue
        if end is not None and stamp >= end:
            continue
        try:
            values.append(float(row[field]))
        except (KeyError, TypeError, ValueError):
            pass
    return values


def main() -> None:
    truths = load_jsonl(REPO / "sample/ground_truth.jsonl", validate_ground_truth)
    case_dirs = sorted(path for path in (REPO / "sample").glob("case_*"))
    results = []
    for truth in truths:
        root = truth["root_cause"]["network_element_id"]
        city, role = root.split("-", 1)
        start, end = truth["_start"], truth["_end"]
        matches = []
        for case_dir in case_dirs:
            nodes = observed_nodes(case_dir, city)
            root_rows = nodes.get(role, [])
            if any(start <= row["timestamp"] < end for row in root_rows):
                matches.append((case_dir, nodes))
        if len(matches) != 1:
            raise ValueError(f"cannot uniquely match public case: {truth['ground_truth_id']}")
        case_dir, nodes = matches[0]
        root_rows = nodes[role]
        category = truth["fault_category"]["sub_category"]
        fields = FIELDS[category]
        detail = []
        for field in fields:
            before = numbers(root_rows, field, None, start)
            during = numbers(root_rows, field, start, end)
            if not before or not during:
                raise ValueError(f"missing before/during observations for {root} {field}")
            detail.append({
                "field": field, "pre_count": len(before), "during_count": len(during),
                "pre_median": median(before), "during_min": min(during),
                "during_median": median(during), "during_max": max(during),
            })
        primary, direction = PRIMARY[category]
        changes = []
        for node, rows in nodes.items():
            before = numbers(rows, primary, None, start)
            during = numbers(rows, primary, start, end)
            if not before or not during:
                continue
            change = max(during) - median(before) if direction == "high" else median(before) - min(during)
            changes.append({"node_id": city + "-" + node, "change": change})
        changes.sort(key=lambda item: item["change"], reverse=True)
        root_rank = next(i + 1 for i, item in enumerate(changes) if item["node_id"] == root)
        results.append({
            "ground_truth_id": truth["ground_truth_id"], "case_directory": case_dir.name,
            "start_time": truth["start_time"], "end_time": truth["end_time"],
            "root_cause": root, "fault_category": truth["fault_category"],
            "primary_metric": primary, "primary_direction": direction,
            "root_primary_change_rank_within_city": root_rank,
            "city_primary_changes": changes, "root_metric_evidence": detail,
            "limitations": "Public case is a short cropped window; pre-event reference has only the rows present in this crop.",
        })
    out = BASE / "results/public_case_evidence.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"public_cases": len(results), "roots": [x["root_cause"] for x in results]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

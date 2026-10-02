"""Compare two unlabeled detection runs without treating either as ground truth."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


NEW_ROUTING = (
    "bgp_route_filter", "ospf6_interface_flap", "route_loop", "long_path_interruption",
)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _category(row: dict) -> tuple[str, str]:
    item = row["fault_category"]
    return item["major_category"], item["sub_category"]


def _roots(row: dict) -> tuple[str, ...]:
    return tuple(item["network_element_id"] for item in row["root_cause_top5"])


def _fingerprint(row: dict) -> tuple:
    return row["start_time"], row["end_time"], _category(row), _roots(row)


def _brief(row: dict) -> dict:
    return {
        "start_time": row["start_time"], "end_time": row["end_time"],
        "fault_category": row["fault_category"], "root_cause_top5": list(_roots(row)),
    }


def compare_run_dirs(baseline_dir: Path, candidate_dir: Path) -> dict:
    baseline_dir, candidate_dir = Path(baseline_dir), Path(candidate_dir)
    old = _read_jsonl(baseline_dir / "predictions.jsonl")
    new = _read_jsonl(candidate_dir / "predictions.jsonl")
    remaining_old = list(old)
    remaining_new = list(new)
    exact = 0
    for row in old:
        match = next((item for item in remaining_new if _fingerprint(item) == _fingerprint(row)), None)
        if match is not None:
            remaining_old.remove(row)
            remaining_new.remove(match)
            exact += 1

    changed_top5 = changed_category = changed_both = 0
    for row in list(remaining_old):
        match = next((item for item in remaining_new
                      if item["start_time"] == row["start_time"]
                      and item["end_time"] == row["end_time"]
                      and _category(item) == _category(row)), None)
        if match is not None:
            changed_top5 += 1
            remaining_old.remove(row)
            remaining_new.remove(match)
    for row in list(remaining_old):
        match = next((item for item in remaining_new
                      if item["start_time"] == row["start_time"]
                      and item["end_time"] == row["end_time"]
                      and _roots(item) == _roots(row)), None)
        if match is not None:
            changed_category += 1
            remaining_old.remove(row)
            remaining_new.remove(match)
    for row in list(remaining_old):
        match = next((item for item in remaining_new
                      if item["start_time"] == row["start_time"]
                      and item["end_time"] == row["end_time"]), None)
        if match is not None:
            changed_both += 1
            remaining_old.remove(row)
            remaining_new.remove(match)

    def category_counts(rows):
        counts = Counter("/".join(_category(row)) for row in rows)
        return dict(sorted(counts.items()))

    def root_direct(directory):
        audit = _read_jsonl(directory / "audit.jsonl")
        return sum(bool(row.get("root_has_direct_evidence")) for row in audit)

    return {
        "baseline_dir": str(baseline_dir), "candidate_dir": str(candidate_dir),
        "baseline_count": len(old), "candidate_count": len(new),
        "exact_unchanged": exact,
        "same_interval_top5_changed": changed_top5,
        "same_interval_category_changed": changed_category,
        "same_interval_top5_and_category_changed": changed_both,
        "added_count": len(remaining_new), "removed_count": len(remaining_old),
        "added": [_brief(row) for row in remaining_new],
        "removed": [_brief(row) for row in remaining_old],
        "baseline_categories": category_counts(old),
        "candidate_categories": category_counts(new),
        "candidate_new_routing_categories": {
            category: sum(_category(row) == ("routing", category) for row in new)
            for category in NEW_ROUTING
        },
        "baseline_root_has_direct_evidence": root_direct(baseline_dir),
        "candidate_root_has_direct_evidence": root_direct(candidate_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two unlabeled v3 runs")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = compare_run_dirs(args.baseline, args.candidate)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "baseline_count", "candidate_count", "exact_unchanged",
        "same_interval_top5_changed", "same_interval_category_changed",
        "same_interval_top5_and_category_changed",
        "added_count", "removed_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Summarize saved same-feature-store threshold-alignment runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def anchor_counts(summary: dict) -> dict[str, int]:
    return {
        name: int(summary["anchor_status_counts"].get(name, 0))
        for name in ("supported", "unsupported", "not_evaluated", "undetermined")
    }


def compact_summary(summary: dict) -> dict:
    return {
        "candidate_count": int(summary["event_count"]),
        "anchor_status_counts": anchor_counts(summary),
        "category_counts": summary["category_counts"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--current-audit", type=Path,
        default=BASE / "runs/stage1_direct_calibrated_20260927/evidence_audit.json",
    )
    parser.add_argument("--control-audit", type=Path)
    parser.add_argument("--sample-report", type=Path)
    args = parser.parse_args()
    previous_path = BASE / "results/threshold_comparison.json"
    previous = read(previous_path) if previous_path.exists() else {}
    if args.control_audit:
        control = compact_summary(read(args.control_audit)["summary"])
    else:
        control = previous.get("control")
    if control is None:
        parser.error("provide --control-audit or a prior results/threshold_comparison.json")
    current = read(args.current_audit)["summary"]
    if args.sample_report:
        sample_report = read(args.sample_report)
        sample = {key: sample_report[key] for key in ("TP", "FP", "FN", "Total")}
    else:
        sample = previous.get("public_sample")
    if sample is None:
        parser.error("provide --sample-report or a prior results/threshold_comparison.json")
    result = {
        "comparison_scope": "same stage-1 feature store; compact pre-adjustment control and current direct run",
        "control": control,
        "current": compact_summary(current),
        "public_sample": sample,
        "qualification": "Stage-1 outcomes are unlabeled; candidate and anchor changes are not accuracy estimates.",
    }
    (BASE / "results/threshold_comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"control": control["event_count"], "current": current["event_count"]}))


if __name__ == "__main__":
    main()

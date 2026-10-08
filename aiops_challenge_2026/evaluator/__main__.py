from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..schema import load_jsonl, load_predictions_for_evaluation, validate_ground_truth
from .evaluator import evaluate


def main() -> int:
    parser = argparse.ArgumentParser(description="CCF AIOps Challenge 2026 official evaluator")
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path("outputs/evaluator_report.json"))
    args = parser.parse_args()
    truth = load_jsonl(args.ground_truth, validate_ground_truth)
    predictions = load_predictions_for_evaluation(args.predictions)
    report = evaluate(truth, predictions)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for key in ("Total", "AD", "RCA", "Major", "Minor", "TP", "FP", "FN"):
        print(f"{key}: {report[key]:.6f}" if isinstance(report[key], float) else f"{key}: {report[key]}")
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

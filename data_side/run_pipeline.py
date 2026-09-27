"""Regenerate and verify all data-side reports from the current inputs."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


BASE = Path(__file__).resolve().parent
REPO = BASE.parent
RUN = BASE / "runs/stage1_direct_calibrated_20260927"


def run(script: str, *arguments: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(REPO), str(BASE), env.get("PYTHONPATH", "")))
    command = [sys.executable, str(BASE / script), *arguments]
    print("running:", " ".join(command), flush=True)
    subprocess.run(command, cwd=REPO, env=env, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-raw-scan", action="store_true", help="re-read all original CSV files")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--run-dir", type=Path, default=RUN, help="direct-detector prediction and audit artifacts")
    args = parser.parse_args()
    predictions = args.run_dir / "predictions.jsonl"
    inference = args.run_dir / "inference.json.gz"
    audit = args.run_dir / "evidence_audit.json"
    for path in (predictions, inference, audit):
        if not path.exists():
            parser.error(f"missing current direct-detector run artifact: {path}")
    scan_args = ["--workers", str(args.workers)]
    if args.force_raw_scan:
        scan_args.append("--force")
    run("scan_raw_quality.py", *scan_args)
    run("profile_group_missing.py")
    run("find_node_missing_rows.py")
    run("build_reports.py", "--predictions", str(predictions), "--inference", str(inference), "--audit", str(audit))
    run("reconcile_dictionary.py")
    run("analyze_public_cases.py")
    run("compare_threshold_runs.py", "--current-audit", str(audit))
    run("verify_outputs.py")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run the complete AIOps data project.

The command is intentionally explicit about the high-cardinality netflow
switch. It never creates a second copy of an archive and never moves the raw
city directories.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
ROOT = PROJECT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from pipeline.cases import detect_candidate_cases, write_cases
from pipeline.inventory import run_inventory
from pipeline.report import render_report
from pipeline.official_baseline import detect_official_baseline_events, write_official_baseline_events


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the AIOps data inventory, official-Case attribution report and optional exploratory audit")
    parser.add_argument("--include-netflow", action="store_true", help="scan already-extracted netflow files; does not extract archives")
    parser.add_argument("--sigma", type=float, default=5.0, help="numeric anomaly z-score threshold")
    parser.add_argument("--baseline-fraction", type=float, default=0.2, help="fraction of each source time span used for baseline")
    parser.add_argument("--merge-gap-minutes", type=int, default=5, help="merge candidate anomaly buckets within this gap")
    parser.add_argument("--min-bucket-points", type=int, default=2, help="minimum evidence points in one minute bucket")
    parser.add_argument("--report-only", action="store_true", help="reuse existing outputs and regenerate figures/report")
    parser.add_argument("--inventory-only", action="store_true", help="stop after inventory and dictionaries")
    parser.add_argument("--cases-only", action="store_true", help="reuse inventory and only regenerate exploratory candidate Cases")
    parser.add_argument("--exploratory-cases", action="store_true", help="also run the optional exploratory anomaly audit; this does not construct official Cases")
    parser.add_argument("--official-baseline-only", action="store_true", help="run the public five-sigma window detector and write official_baseline_events.json")
    args = parser.parse_args()

    output_dir = PROJECT_DIR / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.official_baseline_only:
        print("[pipeline] running official-baseline-compatible window detector")
        events = detect_official_baseline_events(ROOT, sigma=args.sigma, verbose=True)
        write_official_baseline_events(events, output_dir)
        print("[pipeline] official baseline events:", len(events))
        return 0

    if not args.report_only and not args.cases_only:
        run_inventory(ROOT, output_dir, verbose=True)
        if args.inventory_only:
            print("[pipeline] inventory complete")
            return 0

    if not args.report_only:
        print("[pipeline] detecting candidate Cases; netflow included:", args.include_netflow)
        if args.cases_only or args.exploratory_cases:
            print("[pipeline] detecting exploratory Cases; this does not replace official Case boundaries; netflow included:", args.include_netflow)
            cases = detect_candidate_cases(
                ROOT,
                sigma=args.sigma,
                baseline_fraction=args.baseline_fraction,
                merge_gap_minutes=args.merge_gap_minutes,
                min_bucket_points=args.min_bucket_points,
                include_netflow=args.include_netflow,
                verbose=True,
            )
            write_cases(cases, output_dir)
            print("[pipeline] exploratory candidate Cases:", len(cases))

    from figures.generate_figures import main as generate_figures

    generate_figures()
    report_path = render_report(PROJECT_DIR)
    print("[pipeline] report:", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

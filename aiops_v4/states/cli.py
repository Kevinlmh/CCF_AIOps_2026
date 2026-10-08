"""Offline state-discovery command; outputs remain review candidates."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from .pipeline import discover_states


def main(argv=None):
    parser = argparse.ArgumentParser(description='v4 operating states and candidate events')
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('discover')
    build.add_argument('--windows-dir', type=Path, required=True)
    build.add_argument('--batch', required=True)
    build.add_argument('--output-dir', type=Path, required=True)
    build.add_argument('--reference-start')
    build.add_argument('--reference-end')
    build.add_argument('--min-reference', type=int, default=12)
    build.add_argument('--clusters', type=int, default=3)
    build.add_argument('--mode', choices=('statistics', 'cluster', 'hybrid'), default='hybrid')
    build.add_argument('--stat-threshold', type=float, default=6)
    build.add_argument('--cluster-threshold', type=float, default=3)
    build.add_argument('--rare-fraction', type=float, default=0.1)
    build.add_argument('--minimum-overlap', type=float, default=0.5)
    build.add_argument('--no-rules', action='store_true')
    build.add_argument('--verbose', action='store_true')
    args = build_args = parser.parse_args(argv)
    def progress(event):
        print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
    try:
        summary = discover_states(build_args.windows_dir, args.batch, args.output_dir,
                                  reference_start=args.reference_start, reference_end=args.reference_end,
                                  min_reference=args.min_reference, clusters=args.clusters, mode=args.mode,
                                  stat_threshold=args.stat_threshold, cluster_threshold=args.cluster_threshold,
                                  rare_fraction=args.rare_fraction, minimum_overlap=args.minimum_overlap,
                                  rules_enabled=not args.no_rules, progress=progress if args.verbose else None)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0

#!/usr/bin/env python3
"""Run the reproducible seven-source data audit end to end."""

from __future__ import annotations

import argparse
from pathlib import Path

from pipeline import data_audit, merge_audit_outputs, netflow_audit_fast


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the complete seven-source AIOps data audit")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path("aiops_data_project/outputs/data_audit_latest"))
    parser.add_argument("--workers", type=int, default=4, help="parallel NetFlow city workers")
    args = parser.parse_args()

    root = args.root.resolve()
    output_dir = args.output_dir.resolve()
    parts_dir = output_dir / "_parts"
    six_dir = parts_dir / "six_sources"
    netflow_dir = parts_dir / "netflow_fast"
    data_audit.run_audit(root, six_dir, include_netflow=False)
    netflow_audit_fast.run(root, netflow_dir, args.workers)
    merge_audit_outputs.run(six_dir, netflow_dir, output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

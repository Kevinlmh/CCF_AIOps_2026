"""Inspect raw batches or build a canonical v3 feature store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .data.build import build_raw_feature_store, inspect_raw_sources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--profile", choices=("stage1", "stage2"), default="stage1")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--store", type=Path)
    args = parser.parse_args()
    if args.preflight_only:
        inventory = inspect_raw_sources(args.raw_root, profile=args.profile)
        print(json.dumps({key: value for key, value in inventory.items()
                          if key != "source_files"}, ensure_ascii=False))
        return
    if args.store is None:
        parser.error("--store is required unless --preflight-only")
    path = build_raw_feature_store(args.raw_root, args.store, profile=args.profile)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({
        "store": str(path),
        "source_profile": manifest["source_profile"],
        "format_version": manifest["format_version"],
        "minute_count": manifest["minute_count"],
        "shapes": manifest["shapes"],
        "parser_quality": manifest["parser_audit"]["quality_status"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

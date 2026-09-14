"""Validate formal input inventory and headers without scanning 96 GiB of rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from aiops_challenge_2026.config import load_public_config
from baseline.bian.preprocessing.multisource import (
    SOURCE_ORDER,
    validate_source_file_header,
    validate_source_inventory,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    network = load_public_config("network_elements")
    aliases = {city: city for city in network["cities"]}
    inventory = validate_source_inventory(
        args.data_root,
        aliases,
        expected_cities=network["cities"],
        expected_sources=SOURCE_ORDER,
    )
    files = []
    for city in network["cities"]:
        for source in SOURCE_ORDER:
            path = inventory[city][source]
            fields = validate_source_file_header(path, source)
            files.append(
                {
                    "city": city,
                    "source": source,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "columns": len(fields),
                }
            )
    print(
        json.dumps(
            {
                "valid": True,
                "files": len(files),
                "bytes": sum(item["bytes"] for item in files),
                "by_source": {
                    source: sum(item["bytes"] for item in files if item["source"] == source)
                    for source in SOURCE_ORDER
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Small helpers for reading public processed observations."""

from __future__ import annotations

from datetime import datetime, timezone
import csv
from pathlib import Path
from typing import Iterator


def parse_observation_time(value: str) -> datetime | None:
    value = value.strip().strip('"')
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (result if result.tzinfo else result.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iter_csv_rows(root: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    for path in sorted(root.rglob("*.csv")):
        if path.parent.name != "processed":
            continue
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            for row in csv.DictReader(handle):
                yield path, row

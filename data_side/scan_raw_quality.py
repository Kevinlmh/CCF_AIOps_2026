"""Full streaming field-presence audit of all 56 stage-1 raw CSV files.

The C++ helper keeps memory bounded. Results are cached by file size and mtime
so an interrupted 96 GiB scan can resume without repeating completed files.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

from aiops_challenge_2026.config import load_public_config
from baseline.bian.preprocessing.multisource import iter_source_files
from baseline.bian.preprocessing.observations import city_from_path


BASE = Path(__file__).resolve().parent
REPO = BASE.parent
RAW = REPO / "data/stage1/regions"
MANIFEST = REPO / "data/feature_store/v2/stage1_all_cities_v2_20260926/manifest.json"
CACHE = BASE / ".cache/raw_scan"
RESULTS = BASE / "results"


def helper_binary() -> Path:
    source = BASE / "scan_csv_quality.cpp"
    binary = BASE / ".cache/scan_csv_quality"
    binary.parent.mkdir(parents=True, exist_ok=True)
    if not binary.exists() or binary.stat().st_mtime_ns < source.stat().st_mtime_ns:
        subprocess.run(
            ["clang++", "-O3", "-std=c++17", str(source), "-o", str(binary)],
            check=True,
        )
    return binary


def scan_one(source: str, path: Path, binary: Path, force: bool) -> dict:
    stat = path.stat()
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]
    cache_path = CACHE / f"{key}.json"
    if not force and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached["size_bytes"] == stat.st_size and cached["mtime_ns"] == stat.st_mtime_ns:
            cached["missing_by_field"] = {
                field.removeprefix("\ufeff"): count
                for field, count in cached["missing_by_field"].items()
            }
            cached["cache_hit"] = True
            return cached
    result = subprocess.run([str(binary), str(path)], text=True, capture_output=True, check=True)
    lines = result.stdout.splitlines()
    marker, rows, malformed = lines[0].split("\t")
    if marker != "@":
        raise ValueError(f"invalid scanner output for {path}")
    absent = {}
    for line in lines[1:]:
        field, count = line.split("\t")
        absent[field.removeprefix("\ufeff")] = int(count)
    city = city_from_path(path, {city: city for city in load_public_config("network_elements")["cities"]})
    output = {
        "source": source, "city": city, "path": str(path.resolve()),
        "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        "rows": int(rows), "malformed_rows": int(malformed),
        "missing_by_field": absent,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(output, ensure_ascii=False) + "\n", encoding="utf-8")
    return {**output, "cache_hit": False}


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 8:
        parser.error("--workers must be between 1 and 8")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    audited = {
        str((REPO / key).resolve()): value
        for key, value in manifest["parser_audit"]["file_audit"].items()
    }
    files = list(iter_source_files(RAW))
    if len(files) != 56:
        raise ValueError(f"expected 56 source files, found {len(files)}")
    binary = helper_binary()
    start = time.monotonic()
    scanned = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(scan_one, source, path, binary, args.force): (source, path)
            for source, path in files
        }
        for future in as_completed(futures):
            source, path = futures[future]
            item = future.result()
            scanned.append(item)
            print(f"[{len(scanned)}/{len(files)}] {item['city']} {source}: {item['rows']} rows", flush=True)
    scanned.sort(key=lambda item: (item["city"], item["source"]))
    file_rows = []
    field_rows = []
    source_rows = Counter()
    source_missing: dict[tuple[str, str], int] = defaultdict(int)
    for item in scanned:
        audit = audited.get(item["path"])
        if audit is None or audit["rows"] != item["rows"]:
            raise ValueError(f"raw scan disagrees with build audit: {item['path']}")
        if item["malformed_rows"]:
            raise ValueError(f"malformed CSV records in {item['path']}: {item['malformed_rows']}")
        source_rows[item["source"]] += item["rows"]
        file_rows.append({
            "city": item["city"], "source": item["source"],
            "path": str(Path(item["path"]).relative_to(REPO)),
            "rows": item["rows"], "malformed_rows": item["malformed_rows"],
            "size_bytes": item["size_bytes"],
        })
        for field, absent in sorted(item["missing_by_field"].items()):
            source_missing[(item["source"], field)] += absent
            field_rows.append({
                "city": item["city"], "source": item["source"], "field": field,
                "rows": item["rows"], "missing": absent,
                "non_missing": item["rows"] - absent,
                "missing_fraction": round(absent / item["rows"], 8) if item["rows"] else None,
            })
    if dict(source_rows) != manifest["parser_audit"]["rows_by_source"]:
        raise ValueError("source row totals disagree with build audit")
    write_csv(RESULTS / "raw_file_rescan.csv", (
        "city", "source", "path", "rows", "malformed_rows", "size_bytes",
    ), file_rows)
    write_csv(RESULTS / "raw_field_quality.csv", (
        "city", "source", "field", "rows", "missing", "non_missing", "missing_fraction",
    ), field_rows)
    aggregate = [
        {
            "source": source, "field": field, "rows": source_rows[source],
            "missing": missing, "non_missing": source_rows[source] - missing,
            "missing_fraction": round(missing / source_rows[source], 8),
        }
        for (source, field), missing in sorted(source_missing.items())
    ]
    write_csv(RESULTS / "raw_field_quality_by_source.csv", (
        "source", "field", "rows", "missing", "non_missing", "missing_fraction",
    ), aggregate)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": len(scanned), "rows": sum(source_rows.values()),
        "field_rows": len(field_rows), "source_fields": len(aggregate),
        "malformed_records": sum(item["malformed_rows"] for item in scanned),
        "source_rows": dict(source_rows),
        "files_read_this_run": sum(not item["cache_hit"] for item in scanned),
        "files_from_cache": sum(item["cache_hit"] for item in scanned),
        "elapsed_seconds_this_run": round(time.monotonic() - start, 1),
        "method": "C++ streaming CSV field-presence scan, cached by size and mtime; count cross-checked with feature-store parser audit",
    }
    (RESULTS / "raw_rescan_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Fast, reproducible NetFlow-only quality audit.

The full six-source audit keeps richer per-row state because the extracted
files are small enough to do so.  NetFlow expands to several GB per city, so
this companion audit deliberately keeps only the P0 outputs: coverage,
missing-vs-zero field behavior, adjacent duplicate checks, per-series gaps,
entity/interface coverage, and directly observed NetFlow relations.
It never writes or modifies the raw archives.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from . import data_audit as da
except (ImportError, ValueError):
    try:
        from aiops_data_project.pipeline import data_audit as da
    except ModuleNotFoundError:  # direct execution from the pipeline directory
        import data_audit as da


UTC = timezone.utc
SOURCE = "netflow_5tuple"
NUMERIC_FIELDS = set(da.SOURCE_SPECS[SOURCE]["numeric_fields"])


def empty_field_stats() -> Dict[str, Any]:
    return {"rows": 0, "missing": 0, "zero": 0, "numeric": 0, "invalid_numeric": 0, "minimum": None, "maximum": None}


def update_field_stat(stat: Dict[str, Any], raw: str, numeric_expected: bool) -> None:
    stat["rows"] += 1
    if da.is_missing(raw):
        stat["missing"] += 1
        return
    if not numeric_expected:
        return
    value = da.parse_number(raw)
    if value is None:
        stat["invalid_numeric"] += 1
        return
    stat["numeric"] += 1
    if value == 0:
        stat["zero"] += 1
    stat["minimum"] = value if stat["minimum"] is None else min(stat["minimum"], value)
    stat["maximum"] = value if stat["maximum"] is None else max(stat["maximum"], value)


def audit_archive(archive: Dict[str, Any]) -> Dict[str, Any]:
    city = archive["city"]
    registry: Dict[str, Dict[str, Any]] = {}
    entity_minutes: Dict[str, set[int]] = defaultdict(set)
    entity_rows: Dict[str, int] = defaultdict(int)
    entity_first: Dict[str, int] = {}
    entity_last: Dict[str, int] = {}
    series_rows: Dict[str, int] = defaultdict(int)
    series_last: Dict[str, int] = {}
    gap_minutes: Dict[str, int] = defaultdict(int)
    gap_runs: Dict[str, int] = defaultdict(int)
    max_gap: Dict[str, int] = defaultdict(int)
    field_stats: Dict[str, Dict[str, Any]] = {}
    relations: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]] = {}

    rows = 0
    malformed = 0
    parse_failures = 0
    out_of_range = 0
    backward_jumps = 0
    duplicate_exact = 0
    duplicate_semantic = 0
    observed_minutes: set[int] = set()
    first_minute: Optional[int] = None
    last_minute: Optional[int] = None
    previous_timestamp_text = ""
    previous_exact: Optional[Tuple[str, ...]] = None
    previous_semantic: Optional[Tuple[str, ...]] = None
    timestamp_cache: Dict[str, Optional[int]] = {}

    def add_relation(relation_type: str, left_type: str, left_id: str, right_type: str, right_id: str, raw_fields: str, reason: str) -> None:
        key = (relation_type, left_type, left_id, right_type, right_id, city)
        if key not in relations:
            relations[key] = {
                "relation_type": relation_type,
                "left_type": left_type,
                "left_id": left_id,
                "right_type": right_type,
                "right_id": right_id,
                "city": city,
                "sources": {SOURCE},
                "raw_fields": {raw_fields},
                "provenance": {"observed+derived"},
                "confidence": "high",
                "reason": reason,
            }

    iterator = da.iter_archive_csv(archive)
    for actual_city, handle, member in iterator:
        try:
            reader = csv.reader(handle)
            header = next(reader, [])
            field_stats = {name: empty_field_stats() for name in header}
            index = {name: position for position, name in enumerate(header)}

            def get(row: Sequence[str], name: str) -> str:
                position = index.get(name)
                return row[position] if position is not None and position < len(row) else ""

            for row in reader:
                rows += 1
                if len(row) != len(header):
                    malformed += 1
                padded = list(row[:len(header)]) + [""] * max(0, len(header) - len(row))
                for name, raw in zip(header, padded):
                    update_field_stat(field_stats[name], raw, name in NUMERIC_FIELDS)

                timestamp_text = get(padded, "minute_utc")
                if timestamp_text not in timestamp_cache:
                    parsed = da.parse_time(timestamp_text)
                    timestamp_cache[timestamp_text] = int(parsed.timestamp() // 60) if parsed else None
                minute = timestamp_cache[timestamp_text]
                if minute is None:
                    if not da.is_missing(timestamp_text):
                        parse_failures += 1
                    continue
                first_minute = minute if first_minute is None else min(first_minute, minute)
                last_minute = minute if last_minute is None else max(last_minute, minute)
                observed_minutes.add(minute)
                if minute < int(da.DATA_START.timestamp() // 60) or minute >= int(da.DATA_END.timestamp() // 60):
                    out_of_range += 1
                if previous_timestamp_text and timestamp_text < previous_timestamp_text:
                    backward_jumps += 1
                previous_timestamp_text = timestamp_text

                exact = tuple(padded)
                semantic = tuple(get(padded, name) for name in (
                    "minute_utc", "node", "interface_id", "protocol", "src_addr", "src_port", "dst_addr", "dst_port", "collector_port"
                ))
                if previous_exact == exact:
                    duplicate_exact += 1
                if previous_semantic == semantic:
                    duplicate_semantic += 1
                previous_exact = exact
                previous_semantic = semantic

                raw_node = da.clean(get(padded, "node") or get(padded, "node_key"))
                entity = da.canonical_node(city, raw_node) or raw_node or f"{city}:unknown_node"
                interface_id = da.clean(get(padded, "interface_id"))
                series = f"{entity}|{interface_id}"
                entity_rows[entity] += 1
                entity_minutes[entity].add(minute)
                entity_first[entity] = minute if entity not in entity_first else min(entity_first[entity], minute)
                entity_last[entity] = minute if entity not in entity_last else max(entity_last[entity], minute)
                series_rows[series] += 1
                previous_series = series_last.get(series)
                if previous_series is not None:
                    delta = minute - previous_series
                    if delta > 1:
                        missing = delta - 1
                        gap_minutes[series] += missing
                        gap_runs[series] += 1
                        max_gap[series] = max(max_gap[series], missing)
                    elif delta < 0:
                        backward_jumps += 1
                series_last[series] = minute

                item = registry.setdefault(entity, {
                    "entity_type": "network_element",
                    "entity_id": entity,
                    "city": city,
                    "role": entity.split("-", 1)[1] if "-" in entity else entity,
                    "sources": {SOURCE},
                    "raw_aliases": set(),
                    "provenance": {"derived_from_raw_identifier"},
                })
                for raw_value in (get(padded, "node"), get(padded, "node_key"), get(padded, "interface_id")):
                    if da.clean(raw_value):
                        item["raw_aliases"].add(da.clean(raw_value))
                node_key = da.clean(get(padded, "node_key"))
                if node_key:
                    add_relation("has_netflow_alias", "network_element", entity, "netflow_node_key", node_key, "node|node_key", "NetFlow node and node_key occur in the same raw record")
                if interface_id and not da.is_missing(interface_id):
                    add_relation("observed_netflow_interface", "network_element", entity, "interface", f"{entity}:{interface_id}", "node|interface_id|if_role", "NetFlow interface occurs in the same raw record")
        finally:
            handle.close()
        break
    iterator.close()

    span = max(0, last_minute - first_minute + 1) if first_minute is not None and last_minute is not None else 0
    quality = {
        "city": city,
        "source": SOURCE,
        "storage": "compressed_stream",
        "path": f"{archive['paths'][0]}::{member}",
        "member": member,
        "rows": rows,
        "first_timestamp_utc": datetime.fromtimestamp(first_minute * 60, UTC).isoformat().replace("+00:00", "Z") if first_minute is not None else None,
        "last_timestamp_utc": datetime.fromtimestamp(last_minute * 60, UTC).isoformat().replace("+00:00", "Z") if last_minute is not None else None,
        "span_minutes": span,
        "observed_minutes": len(observed_minutes),
        "expected_minutes_global": da.EXPECTED_MINUTES,
        "missing_minutes_global": max(0, da.EXPECTED_MINUTES - len(observed_minutes)),
        "missing_minutes_internal": max(0, span - len(observed_minutes)),
        "timestamp_parse_failures": parse_failures,
        "malformed_rows": malformed,
        "out_of_expected_range": out_of_range,
        "backward_jumps": backward_jumps,
        "duplicate_exact_adjacent": duplicate_exact,
        "duplicate_semantic_adjacent": duplicate_semantic,
        "entity_count": len(entity_rows),
        "series_count": len(series_rows),
    }
    entity_output = []
    for entity in sorted(entity_rows):
        first = entity_first[entity]
        last = entity_last[entity]
        entity_output.append({
            "city": city,
            "source": SOURCE,
            "entity_id": entity,
            "rows": entity_rows[entity],
            "first_timestamp_utc": datetime.fromtimestamp(first * 60, UTC).isoformat().replace("+00:00", "Z"),
            "last_timestamp_utc": datetime.fromtimestamp(last * 60, UTC).isoformat().replace("+00:00", "Z"),
            "observed_minutes": len(entity_minutes[entity]),
            "span_minutes": last - first + 1,
            "missing_minutes_internal": max(0, last - first + 1 - len(entity_minutes[entity])),
            "series_count": sum(1 for key in series_rows if key == entity or key.startswith(entity + "|")),
        })
    field_output = []
    for name, stat in field_stats.items():
        field_output.append({
            "city": city, "source": SOURCE, "field": name,
            "rows": stat["rows"], "missing": stat["missing"],
            "missing_ratio": round(stat["missing"] / stat["rows"], 8) if stat["rows"] else None,
            "numeric": stat["numeric"], "zero": stat["zero"],
            "zero_ratio_nonmissing_numeric": round(stat["zero"] / stat["numeric"], 8) if stat["numeric"] else None,
            "invalid_numeric": stat["invalid_numeric"], "minimum": stat["minimum"], "maximum": stat["maximum"],
            "numeric_field_expected": name in NUMERIC_FIELDS,
        })
    gap_output = [{
        "city": city, "source": SOURCE, "series_key": key, "missing_minutes": gap_minutes[key],
        "gap_runs": gap_runs[key], "max_single_gap_minutes": max_gap[key],
    } for key in sorted(gap_minutes)]
    registry_output = []
    for item in sorted(registry.values(), key=lambda value: value["entity_id"]):
        registry_output.append({
            "entity_type": item["entity_type"], "entity_id": item["entity_id"], "city": item["city"],
            "role": item["role"], "sources": "|".join(sorted(item["sources"])),
            "raw_aliases": "|".join(sorted(item["raw_aliases"])), "provenance": "|".join(sorted(item["provenance"])),
        })
    relation_output = []
    for item in sorted(relations.values(), key=lambda value: (value["relation_type"], value["left_id"], value["right_id"])):
        relation_output.append({
            "relation_type": item["relation_type"], "left_type": item["left_type"], "left_id": item["left_id"],
            "right_type": item["right_type"], "right_id": item["right_id"], "city": item["city"],
            "sources": "|".join(sorted(item["sources"])), "raw_fields": "|".join(sorted(item["raw_fields"])),
            "provenance": "|".join(sorted(item["provenance"])), "confidence": item["confidence"], "reason": item["reason"],
        })
    source_summary = {
        "source": SOURCE, "files": 1, "cities": 1, "rows": rows, "storage": "compressed_stream",
        "first_timestamp_utc": quality["first_timestamp_utc"], "last_timestamp_utc": quality["last_timestamp_utc"],
        "missing_minutes_global_total": quality["missing_minutes_global"], "malformed_rows": malformed,
        "timestamp_parse_failures": parse_failures, "duplicate_exact_adjacent": duplicate_exact,
        "duplicate_semantic_adjacent": duplicate_semantic,
    }
    return {
        "city": city, "quality": quality, "entities": entity_output, "fields": field_output,
        "gaps": gap_output, "registry": registry_output, "relations": relation_output,
        "source_summary": source_summary,
    }


def write_csv(path: Path, rows: Iterable[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run(root: Path, output_dir: Path, workers: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    archives = da.discover_archives(root)
    results: List[Dict[str, Any]] = []
    max_workers = max(1, min(workers, len(archives)))
    print(f"[netflow-fast] workers={max_workers} cities={len(archives)}", flush=True)
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(audit_archive, archive): archive for archive in archives}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"[netflow-fast] complete {result['city']} rows={result['quality']['rows']}", flush=True)
    results.sort(key=lambda result: result["city"])
    quality = [result["quality"] for result in results]
    entities = [row for result in results for row in result["entities"]]
    fields = [row for result in results for row in result["fields"]]
    gaps = [row for result in results for row in result["gaps"]]
    registry = [row for result in results for row in result["registry"]]
    relations = [row for result in results for row in result["relations"]]
    summary = [result["source_summary"] for result in results]
    write_csv(output_dir / "netflow_quality_by_city.csv", quality, list(quality[0].keys()))
    write_csv(output_dir / "netflow_entity_coverage.csv", entities, list(entities[0].keys()) if entities else [])
    write_csv(output_dir / "netflow_field_quality.csv", fields, list(fields[0].keys()) if fields else [])
    write_csv(output_dir / "netflow_time_gaps.csv", gaps, list(gaps[0].keys()) if gaps else ["city", "source", "series_key", "missing_minutes", "gap_runs", "max_single_gap_minutes"])
    write_csv(output_dir / "netflow_entity_registry.csv", registry, list(registry[0].keys()) if registry else [])
    write_csv(output_dir / "netflow_relations.csv", relations, list(relations[0].keys()) if relations else [])
    write_csv(output_dir / "netflow_source_summary.csv", summary, list(summary[0].keys()))
    manifest = {
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "raw_root": str(root), "output_dir": str(output_dir), "workers": max_workers,
        "source": SOURCE, "cities": [result["city"] for result in results],
        "rows": sum(result["quality"]["rows"] for result in results),
        "files": [result["quality"] for result in results],
        "limitations": [
            "NetFlow counters are bucket measures; no cumulative counter-delta audit is applied",
            "duplicate checks are adjacent-row checks in the archive member order",
            "entity relations are directly observed or deterministically derived from the same NetFlow row",
            "no normal/fault labels are created",
        ],
    }
    (output_dir / "netflow_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast full-city NetFlow quality audit")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    run(args.root.resolve(), args.output_dir.resolve(), args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

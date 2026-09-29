"""Streaming inventory and schema profiling for the supplied AIOps data.

The scanner intentionally uses the Python standard library and processes one
CSV row at a time. The dataset contains multi-gigabyte files, so loading a
whole table into pandas would make the inventory less reproducible and less
portable.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .definitions import (
    FIELD_DEFINITIONS,
    SOURCE_DEFINITIONS,
    infer_traffic_field,
    source_definition,
)


NULL_TOKENS = {"", "NULL", "null", "\\N", "NA", "N/A", "nan", "NaN"}
DATE_RE = re.compile(r"^([^_]+)_20260819040000_20260902040000$")


def parse_time(value: str) -> Optional[datetime]:
    value = (value or "").strip().strip('"')
    if not value or value in NULL_TOKENS:
        return None
    value = value.replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def city_from_region_dir(path: Path) -> str:
    match = DATE_RE.match(path.name)
    if match:
        return match.group(1)
    return path.name.split("_2026", 1)[0]


def discover_region_data_dirs(root: Path) -> List[Tuple[str, Path]]:
    found = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        data_dirs = sorted(item for item in child.iterdir() if item.is_dir() and item.name.endswith("_data"))
        for data_dir in data_dirs:
            found.append((city_from_region_dir(child), data_dir))
    return found


def source_from_filename(name: str) -> Optional[str]:
    if name.startswith("node_metrics_"):
        return "node_metrics"
    if name.startswith("interface_metrics_"):
        return "interface_metrics"
    if name.startswith("routing_metrics_"):
        return "routing_metrics"
    if name.startswith("scrape_health_"):
        return "scrape_health"
    if name.startswith("frr_syslog_events_"):
        return "frr_syslog_events"
    if name == "traffic_flow_metrics.csv":
        return "traffic_flow_metrics"
    if name == "netflow_5tuple_minute_readable.csv":
        return "netflow_5tuple"
    return None


def _safe_value(value: str) -> str:
    return (value or "").strip().strip('"')


def _file_profile(city: str, source: str, path: Path) -> Dict[str, object]:
    definition = source_definition(source)
    timestamp_field = str(definition["timestamp_field"])
    row_count = 0
    malformed_rows = 0
    first_time: Optional[datetime] = None
    last_time: Optional[datetime] = None
    field_stats: Dict[str, Dict[str, object]] = {}
    entity_values: Dict[str, set] = defaultdict(set)
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        for field in header:
            field_stats[field] = {"missing": 0, "non_missing": 0, "distinct_sample": []}
        entity_fields = set(str(item) for item in definition.get("entity_fields", []))
        for row in reader:
            row_count += 1
            if len(row) != len(header):
                malformed_rows += 1
            value = parse_time(row.get(timestamp_field, ""))
            if value is not None:
                first_time = value if first_time is None or value < first_time else first_time
                last_time = value if last_time is None or value > last_time else last_time
            for field in header:
                raw = _safe_value(row.get(field, ""))
                stats = field_stats[field]
                if raw in NULL_TOKENS:
                    stats["missing"] = int(stats["missing"]) + 1
                else:
                    stats["non_missing"] = int(stats["non_missing"]) + 1
                    distinct = stats["distinct_sample"]
                    if len(distinct) < 20 and raw not in distinct:
                        distinct.append(raw)
                if field in entity_fields and len(entity_values[field]) < 2000:
                    entity_values[field].add(raw)
    for field, stats in field_stats.items():
        stats["missing_ratio"] = round(float(stats["missing"]) / row_count, 6) if row_count else None
    return {
        "city": city,
        "source": source,
        "path": str(path),
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "rows": row_count,
        "columns": len(header),
        "header": header,
        "timestamp_field": timestamp_field,
        "first_timestamp_utc": first_time.isoformat().replace("+00:00", "Z") if first_time else None,
        "last_timestamp_utc": last_time.isoformat().replace("+00:00", "Z") if last_time else None,
        "malformed_rows": malformed_rows,
        "entity_cardinality": {key: len(value) for key, value in sorted(entity_values.items())},
        "entity_samples": {key: sorted(value)[:20] for key, value in sorted(entity_values.items())},
        "field_stats": field_stats,
    }


def _archive_manifest(root: Path) -> List[Dict[str, object]]:
    records = []
    for path in sorted(root.glob("*.tar.gz*")):
        records.append({
            "path": str(path),
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "split_volume": path.suffix in {".aa", ".ab"},
        })
    return records


def _field_dictionary_from_header(source: str, field: str) -> Dict[str, str]:
    if source == "traffic_flow_metrics":
        definition = infer_traffic_field(field)
    else:
        definition = FIELD_DEFINITIONS.get(source, {}).get(field, {})
    return {
        "source": source,
        "field": field,
        "type": str(definition.get("type", "unknown")),
        "unit": str(definition.get("unit", "metric-specific")),
        "meaning": str(definition.get("meaning", "field requires further source validation")),
        "analytical_role": str(definition.get("role", "additional evidence")),
        "raw_missing_tokens": "empty|NULL|\\N|NA|N/A|nan",
    }


def build_field_dictionary(file_records: Iterable[Dict[str, object]]) -> List[Dict[str, str]]:
    headers: Dict[str, List[str]] = defaultdict(list)
    for record in file_records:
        source = str(record["source"])
        for field in record.get("header", []):
            field = str(field)
            if field not in headers[source]:
                headers[source].append(field)
    return [
        _field_dictionary_from_header(source, field)
        for source in sorted(headers)
        for field in headers[source]
    ]


def build_schema_reference() -> List[Dict[str, str]]:
    """Expose explicit schema definitions for sources not currently materialized.

    In particular, netflow remains compressed-only to respect the workspace
    storage budget. Keeping its contract in a separate artifact prevents an
    unobserved schema from being confused with an inventory measured from raw
    rows.
    """
    rows = []
    for source, definitions in sorted(FIELD_DEFINITIONS.items()):
        for field, definition in definitions.items():
            rows.append({
                "source": source,
                "field": field,
                "type": str(definition.get("type", "unknown")),
                "unit": str(definition.get("unit", "metric-specific")),
                "meaning": str(definition.get("meaning", "field requires further source validation")),
                "analytical_role": str(definition.get("role", "additional evidence")),
                "schema_status": "definition_reference_only",
                "raw_missing_tokens": "empty|NULL|\\N|NA|N/A|nan",
            })
    return rows


def _json_dump(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def run_inventory(root: Path, output_dir: Path, verbose: bool = True) -> Dict[str, object]:
    """Scan all extracted CSVs and write machine-readable inventory artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    file_records: List[Dict[str, object]] = []
    discovered = discover_region_data_dirs(root)
    for city, data_dir in discovered:
        for path in sorted(data_dir.glob("*.csv")):
            source = source_from_filename(path.name)
            if source is None:
                continue
            if verbose:
                print("[inventory] scanning", path)
            file_records.append(_file_profile(city, source, path))
    extracted_sources = {str(item["source"]) for item in file_records}
    source_summary = []
    for definition in SOURCE_DEFINITIONS:
        source = str(definition["source"])
        records = [item for item in file_records if item["source"] == source]
        source_summary.append({
            "source": source,
            "origin": definition["origin"],
            "grain": definition["grain"],
            "timestamp_field": definition["timestamp_field"],
            "files": len(records),
            "cities": len({str(item["city"]) for item in records}),
            "rows": sum(int(item["rows"]) for item in records),
            "size_bytes": sum(int(item["size_bytes"]) for item in records),
            "columns": max((int(item["columns"]) for item in records), default=None),
            "storage_status": "extracted" if records else "compressed_only_or_missing",
            "primary_fault_families": list(definition["primary_fault_families"]),
        })
    payload = {
        "project_root": str(root),
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "regions_found": sorted({city for city, _ in discovered}),
        "region_data_dirs": [{"city": city, "path": str(path)} for city, path in discovered],
        "archives": _archive_manifest(root),
        "source_summary": source_summary,
        "files": file_records,
        "notes": [
            "Rows are data rows and exclude the header.",
            "Naive timestamps are interpreted as UTC according to the public project README.",
            "netflow is listed as compressed_only_or_missing when it has not been expanded.",
        ],
    }
    _json_dump(output_dir / "inventory.json", payload)
    summary_rows = []
    for item in source_summary:
        summary_rows.append({key: value for key, value in item.items() if key != "primary_fault_families"})
    _write_csv(output_dir / "source_summary.csv", summary_rows)
    file_rows = []
    for item in file_records:
        file_rows.append({
            key: item[key]
            for key in ("city", "source", "path", "filename", "size_bytes", "rows", "columns", "timestamp_field", "first_timestamp_utc", "last_timestamp_utc", "malformed_rows")
        })
    _write_csv(output_dir / "file_inventory.csv", file_rows)
    field_dictionary = build_field_dictionary(file_records)
    _json_dump(output_dir / "field_dictionary.json", field_dictionary)
    _write_csv(output_dir / "field_dictionary.csv", field_dictionary)
    schema_reference = build_schema_reference()
    _json_dump(output_dir / "schema_reference.json", schema_reference)
    _write_csv(output_dir / "schema_reference.csv", schema_reference)
    source_mapping = []
    for definition in SOURCE_DEFINITIONS:
        source_mapping.append({
            "source": definition["source"],
            "filename_glob": definition["filename_glob"],
            "origin": definition["origin"],
            "grain": definition["grain"],
            "entity_fields": "|".join(str(item) for item in definition["entity_fields"]),
            "primary_fault_families": "|".join(str(item) for item in definition["primary_fault_families"]),
            "baseline_role": definition["baseline_role"],
        })
    _write_csv(output_dir / "source_mapping.csv", source_mapping)
    return payload

#!/usr/bin/env python3
"""Merge the validated six-source audit with the completed NetFlow audit."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple


UTC = timezone.utc


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Iterable[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def integer(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def split_values(value: Any) -> set[str]:
    return {item for item in str(value or "").split("|") if item}


def merge_pipe_field(left: str, right: str) -> str:
    return "|".join(sorted(split_values(left) | split_values(right)))


def merge_registry(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    merged: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in rows:
        key = (row.get("entity_type", ""), row.get("entity_id", ""))
        current = merged.setdefault(key, dict(row))
        for name in ("sources", "raw_aliases", "provenance"):
            current[name] = merge_pipe_field(current.get(name, ""), row.get(name, ""))
        if current.get("entity_type") == "network_element":
            entity_id = current.get("entity_id", "")
            current["role"] = entity_id.split("-", 1)[1] if "-" in entity_id else entity_id
    return sorted(merged.values(), key=lambda row: (row.get("entity_type", ""), row.get("entity_id", "")))


def merge_relations(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    merged: Dict[Tuple[str, str, str, str, str, str], Dict[str, str]] = {}
    for row in rows:
        key = tuple(row.get(name, "") for name in ("relation_type", "left_type", "left_id", "right_type", "right_id", "city"))
        current = merged.setdefault(key, dict(row))
        for name in ("sources", "raw_fields", "provenance"):
            current[name] = merge_pipe_field(current.get(name, ""), row.get(name, ""))
    return sorted(merged.values(), key=lambda row: (row.get("relation_type", ""), row.get("left_id", ""), row.get("right_id", ""), row.get("city", "")))


def rebuild_service_mapping(relations: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    targets = {
        (row.get("city", ""), row.get("left_id", "")): row.get("right_id", "")
        for row in relations
        if row.get("relation_type") == "targets_city"
    }
    result = []
    for row in relations:
        if row.get("relation_type") != "observes_service":
            continue
        result.append({
            "source_city": row.get("city", ""),
            "observer_id": row.get("left_id", ""),
            "target_domain": row.get("right_id", ""),
            "target_city": targets.get((row.get("city", ""), row.get("right_id", "")), ""),
            "source": row.get("sources", ""),
            "raw_fields": row.get("raw_fields", ""),
            "provenance": row.get("provenance", ""),
            "confidence": row.get("confidence", ""),
            "reason": row.get("reason", ""),
        })
    return sorted(result, key=lambda row: (row["source_city"], row["observer_id"], row["target_domain"]))


def rebuild_source_summary(quality_rows: Sequence[Dict[str, str]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in quality_rows:
        grouped[row.get("source", "")].append(row)
    result = []
    for source, rows in sorted(grouped.items()):
        result.append({
            "source": source,
            "files": len(rows),
            "cities": len({row.get("city", "") for row in rows}),
            "rows": sum(integer(row.get("rows")) for row in rows),
            "storage": "|".join(sorted({row.get("storage", "") for row in rows})),
            "first_timestamp_utc": min((row.get("first_timestamp_utc") for row in rows if row.get("first_timestamp_utc")), default=""),
            "last_timestamp_utc": max((row.get("last_timestamp_utc") for row in rows if row.get("last_timestamp_utc")), default=""),
            "missing_minutes_global_total": sum(integer(row.get("missing_minutes_global")) for row in rows),
            "malformed_rows": sum(integer(row.get("malformed_rows")) for row in rows),
            "timestamp_parse_failures": sum(integer(row.get("timestamp_parse_failures")) for row in rows),
            "duplicate_exact_adjacent": sum(integer(row.get("duplicate_exact_adjacent")) for row in rows),
            "duplicate_semantic_adjacent": sum(integer(row.get("duplicate_semantic_adjacent")) for row in rows),
        })
    return result


def update_semantics(semantics: List[Dict[str, str]], fields: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    for semantic in semantics:
        expected = semantic.get("field", "")
        matches = [
            row for row in fields
            if row.get("source") == semantic.get("source")
            and (row.get("field") == expected if not expected.startswith("*_") else row.get("field", "").endswith(expected[1:]))
        ]
        if not matches:
            continue
        minimums = [number(row.get("minimum")) for row in matches if row.get("minimum") not in (None, "", "None")]
        maximums = [number(row.get("maximum")) for row in matches if row.get("maximum") not in (None, "", "None")]
        missing = [number(row.get("missing_ratio")) for row in matches if row.get("missing_ratio") not in (None, "", "None")]
        semantic["observed_min_across_files"] = min(minimums) if minimums else ""
        semantic["observed_max_across_files"] = max(maximums) if maximums else ""
        semantic["max_file_missing_ratio"] = max(missing) if missing else ""
        semantic["semantic_status"] = "verified_from_schema_and_observed_values"
    return semantics


def run(base_dir: Path, netflow_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    quality = read_csv(base_dir / "quality_by_city_source.csv") + read_csv(netflow_dir / "netflow_quality_by_city.csv")
    entities = read_csv(base_dir / "entity_coverage.csv") + read_csv(netflow_dir / "netflow_entity_coverage.csv")
    fields = read_csv(base_dir / "field_quality.csv") + read_csv(netflow_dir / "netflow_field_quality.csv")
    gaps = read_csv(base_dir / "time_gaps.csv") + read_csv(netflow_dir / "netflow_time_gaps.csv")
    registries = merge_registry(read_csv(base_dir / "entity_registry.csv") + read_csv(netflow_dir / "netflow_entity_registry.csv"))
    relations = merge_relations(read_csv(base_dir / "entity_relations.csv") + read_csv(netflow_dir / "netflow_relations.csv"))
    semantics = update_semantics(read_csv(base_dir / "metric_semantics.csv"), fields)
    source_summary = rebuild_source_summary(quality)
    service_mapping = rebuild_service_mapping(relations)
    counters = read_csv(base_dir / "counter_audit.csv")

    write_csv(output_dir / "quality_by_city_source.csv", quality, list(quality[0].keys()))
    write_csv(output_dir / "entity_coverage.csv", entities, list(entities[0].keys()))
    write_csv(output_dir / "field_quality.csv", fields, list(fields[0].keys()))
    write_csv(output_dir / "time_gaps.csv", gaps, list(gaps[0].keys()) if gaps else ["city", "source", "series_key", "missing_minutes", "gap_runs", "max_single_gap_minutes"])
    write_csv(output_dir / "counter_audit.csv", counters, list(counters[0].keys()) if counters else ["city", "source", "series_key", "field"])
    write_csv(output_dir / "entity_registry.csv", registries, list(registries[0].keys()))
    write_csv(output_dir / "entity_relations.csv", relations, list(relations[0].keys()))
    write_csv(output_dir / "service_probe_mapping.csv", service_mapping, list(service_mapping[0].keys()) if service_mapping else ["source_city", "observer_id", "target_domain", "target_city", "source", "raw_fields", "provenance", "confidence", "reason"])
    write_csv(output_dir / "metric_semantics.csv", semantics, list(semantics[0].keys()))
    write_csv(output_dir / "source_summary.csv", source_summary, list(source_summary[0].keys()))

    netflow_manifest = json.loads((netflow_dir / "netflow_manifest.json").read_text(encoding="utf-8"))
    base_manifest = json.loads((base_dir / "audit_manifest.json").read_text(encoding="utf-8"))
    artifacts = [
        "quality_by_city_source.csv", "entity_coverage.csv", "field_quality.csv", "time_gaps.csv",
        "counter_audit.csv", "entity_registry.csv", "entity_relations.csv", "service_probe_mapping.csv",
        "metric_semantics.csv", "source_summary.csv", "audit_manifest.json", "audit_detail.json",
    ]
    manifest = {
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "raw_root": base_manifest.get("raw_root", ""), "output_dir": str(output_dir),
        "expected_data_start_utc": base_manifest.get("expected_data_start_utc"),
        "expected_data_end_exclusive_utc": base_manifest.get("expected_data_end_exclusive_utc"),
        "expected_minutes": base_manifest.get("expected_minutes"), "include_netflow": True,
        "netflow_workers": netflow_manifest.get("workers", 0), "source_count": len(source_summary),
        "file_count": len(quality), "row_count": sum(integer(row.get("rows")) for row in quality),
        "city_count": len({row.get("city", "") for row in quality}),
        "limitations": list(base_manifest.get("limitations", [])) + list(netflow_manifest.get("limitations", [])) + [
            "NetFlow was processed with the P0-focused streaming auditor and merged after independent city-level checks",
        ],
        "files": quality, "source_summary": source_summary, "artifacts": artifacts,
    }
    detail = {
        "files": quality, "entities": registries, "relations": relations,
        "service_probe_mapping": service_mapping, "metric_semantics": semantics,
    }
    (output_dir / "audit_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "audit_detail.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "sources": len(source_summary), "files": len(quality), "rows": manifest["row_count"], "cities": manifest["city_count"]}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge six-source and NetFlow AIOps audit artifacts")
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--netflow-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    run(args.base_dir.resolve(), args.netflow_dir.resolve(), args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

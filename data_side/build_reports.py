"""Build reproducible, label-free data-side reports from the existing v2 store.

Run from the repository root. Raw CSVs are opened only for their headers; row
counts and parser errors come from the feature-store build audit.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from aiops_challenge_2026.config import load_public_config
from aiops_v2.data.feature_store import FeatureStore
from baseline.bian.preprocessing.multisource import IDENTITY_FIELDS, iter_source_files
from baseline.bian.preprocessing.observations import city_from_path, normalize_node_id


REPO = Path(__file__).resolve().parent.parent
DEFAULT_RAW = REPO / "data/stage1/regions"
DEFAULT_STORE = REPO / "data/feature_store/v2/stage1_all_cities_v2_20260926"
DEFAULT_OUT = Path(__file__).resolve().parent / "results"
SOURCES = ("node", "interface", "routing", "scrape", "traffic", "netflow", "frr")
TRAFFIC_SUFFIXES = (
    ".requests_rate", ".success_rate", ".error_rate",
    ".latency_p95_seconds", ".loss_rate", ".throughput_bps",
)


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return str(path.resolve())


def quantile(values: np.ndarray, q: float) -> float | None:
    return float(np.quantile(values, q)) if values.size else None


def rounded(value: float | None) -> float | None:
    return None if value is None else float(f"{value:.8g}")


def file_and_schema_reports(raw_root: Path, manifest: dict, out: Path) -> dict:
    aliases = {city: city for city in load_public_config("network_elements")["cities"]}
    file_audit = manifest["parser_audit"]["file_audit"]
    audited = {str((REPO / path).resolve()): item for path, item in file_audit.items()}
    inventory: list[dict] = []
    fields_by_source: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    headers_by_source: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    found_paths: set[str] = set()
    for source, path in iter_source_files(raw_root):
        resolved = str(path.resolve())
        if resolved not in audited:
            raise ValueError(f"file is absent from build audit: {path}")
        found_paths.add(resolved)
        city = city_from_path(path, aliases)
        if city is None:
            raise ValueError(f"cannot resolve city from {path}")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            header = tuple(next(csv.reader(handle)))
        if not header or len(set(header)) != len(header):
            raise ValueError(f"empty or duplicate CSV header: {path}")
        headers_by_source[source].add(header)
        for field in header:
            fields_by_source[source][field].add(city)
        item = audited[resolved]
        inventory.append({
            "city": city, "source": source, "path": relative(path),
            "size_bytes": path.stat().st_size, "rows_in_build_audit": item["rows"],
            "first_timestamp": item.get("first_timestamp"),
            "last_timestamp": item.get("last_timestamp"),
            "bad_rows": item.get("bad_rows", 0),
            "invalid_numeric_values": item.get("invalid_numeric_values", 0),
            "unmapped_entities": item.get("unmapped_entities", 0),
            "out_of_order_rows": item.get("out_of_order_rows", 0),
            "header_sha256": hashlib.sha256(",".join(header).encode()).hexdigest(),
        })
    if found_paths != set(audited):
        missing = set(audited) - found_paths
        raise ValueError(f"audited source files are missing from raw root: {len(missing)}")
    city_source_counts = Counter((row["city"], row["source"]) for row in inventory)
    expected = {
        (city, source) for city in aliases for source in SOURCES
    }
    if set(city_source_counts) != expected or any(count != 1 for count in city_source_counts.values()):
        raise ValueError("raw file inventory is not exactly one file per city and source")
    totals = Counter()
    for row in inventory:
        totals[row["source"]] += int(row["rows_in_build_audit"])
    if dict(totals) != manifest["parser_audit"]["rows_by_source"]:
        raise ValueError("per-file row counts disagree with parser source totals")
    inventory.sort(key=lambda row: (row["city"], SOURCES.index(row["source"])))
    write_csv(out / "file_inventory.csv", (
        "city", "source", "path", "size_bytes", "rows_in_build_audit",
        "first_timestamp", "last_timestamp", "bad_rows", "invalid_numeric_values",
        "unmapped_entities", "out_of_order_rows", "header_sha256",
    ), inventory)
    schema = [
        {
            "source": source, "field": field,
            "role": "identity_or_time" if field in IDENTITY_FIELDS else "metric_or_payload",
            "city_count": len(cities), "cities": ";".join(sorted(cities)),
        }
        for source, fields in fields_by_source.items()
        for field, cities in fields.items()
    ]
    schema.sort(key=lambda row: (SOURCES.index(row["source"]), row["field"]))
    write_csv(out / "raw_schema.csv", ("source", "field", "role", "city_count", "cities"), schema)
    city_source = [
        {
            "city": row["city"], "source": row["source"],
            "rows": row["rows_in_build_audit"], "size_bytes": row["size_bytes"],
            "first_timestamp": row["first_timestamp"],
            "last_timestamp": row["last_timestamp"],
        }
        for row in inventory
    ]
    write_csv(out / "city_source_coverage.csv", (
        "city", "source", "rows", "size_bytes", "first_timestamp", "last_timestamp",
    ), city_source)
    return {
        "files": len(inventory), "cities": len({row["city"] for row in inventory}),
        "sources": sorted({row["source"] for row in inventory}),
        "schema_field_rows": len(schema),
        "sources_with_header_drift": sorted(
            source for source, headers in headers_by_source.items() if len(headers) > 1
        ),
        "total_rows_in_build_audit": sum(int(row["rows_in_build_audit"]) for row in inventory),
    }


def node_coverage_report(store: FeatureStore, out: Path) -> dict:
    names = store.features.names("node")
    rows: list[dict] = []
    minutes = store.node_values.shape[0]
    entirely_unobserved: list[str] = []
    edge_observed = np.any(store.edge_mask, axis=2)
    log_observed = np.any(store.log_mask, axis=2)
    for index, node in enumerate(store.entities.nodes):
        source_minutes: dict[str, int] = {}
        for source in ("node", "interface", "routing", "scrape"):
            columns = [i for i, name in enumerate(names) if name.startswith(source + ".")]
            observed = int(np.count_nonzero(np.any(store.node_mask[:, index, columns], axis=1)))
            source_minutes[source] = observed
        source_minutes["frr"] = int(np.count_nonzero(log_observed[:, index]))
        for source in ("netflow", "traffic"):
            edges = [
                i for i, edge in enumerate(store.entities.edges)
                if edge.source == node and edge.relation == source
            ]
            source_minutes[source] = int(np.count_nonzero(np.any(edge_observed[:, edges], axis=1))) if edges else 0
        if not any(source_minutes.values()):
            entirely_unobserved.append(node)
        for source in SOURCES:
            observed = source_minutes[source]
            rows.append({
                "node_id": node, "city": node.split("-", 1)[0], "source": source,
                "observed_minutes": observed, "total_minutes": minutes,
                "observed_fraction": rounded(observed / minutes),
            })
    write_csv(out / "entity_source_coverage.csv", (
        "node_id", "city", "source", "observed_minutes", "total_minutes", "observed_fraction",
    ), rows)
    return {"fully_unobserved_nodes": entirely_unobserved}


def raw_node_alias_report(raw_root: Path, manifest: dict, out: Path) -> dict:
    network = load_public_config("network_elements")
    aliases = {city: city for city in network["cities"]}
    roles = tuple(network["device_roles"])
    counts: Counter[tuple[str, str, str, str, str]] = Counter()
    read_rows = 0
    for source, path in iter_source_files(raw_root):
        if source != "node":
            continue
        city = city_from_path(path, aliases)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                raw_region = (row.get("region") or "").strip()
                raw_node = (row.get("node") or "").strip()
                raw_role = (row.get("node_type") or "").strip()
                canonical = normalize_node_id(raw_node, city, roles)
                counts[(city or "", raw_region, raw_node, raw_role, canonical or "")] += 1
                read_rows += 1
    if read_rows != manifest["parser_audit"]["rows_by_source"]["node"]:
        raise ValueError("raw node scan disagrees with parser audit row count")
    rows = [
        {
            "city": city, "raw_region": region, "raw_node": node,
            "raw_node_type": role, "canonical_node_id": canonical, "raw_row_count": count,
        }
        for (city, region, node, role, canonical), count in sorted(counts.items())
    ]
    write_csv(out / "raw_node_aliases.csv", (
        "city", "raw_region", "raw_node", "raw_node_type",
        "canonical_node_id", "raw_row_count",
    ), rows)
    return {"raw_node_alias_rows": len(rows), "raw_node_rows_scanned": read_rows}


def metric_profile_report(store: FeatureStore, out: Path) -> dict:
    cities = tuple(load_public_config("network_elements")["cities"])
    rows: list[dict] = []
    modalities = (
        ("node", store.node_values, store.node_mask, store.features.names("node")),
        ("edge", store.edge_values, store.edge_mask, store.features.names("edge")),
        ("log", store.log_values, store.log_mask, store.features.names("log")),
    )
    for modality, cube, mask_cube, names in modalities:
        for city in cities:
            if modality in {"node", "log"}:
                entity_indexes = [i for i, node in enumerate(store.entities.nodes) if node.startswith(city + "-")]
            else:
                entity_indexes = [
                    i for i, edge in enumerate(store.entities.edges)
                    if edge.relation == "traffic" and edge.target.startswith(f"service-group:{city}:")
                ]
            if not entity_indexes:
                continue
            for feature_index, metric in enumerate(names):
                if modality == "edge" and not (
                    metric.startswith("traffic.") and metric.endswith(TRAFFIC_SUFFIXES)
                ):
                    continue
                value = np.asarray(cube[:, entity_indexes, feature_index], dtype=np.float32)
                observed = np.asarray(mask_cube[:, entity_indexes, feature_index], dtype=bool)
                eligible = np.any(observed, axis=0)
                if not eligible.any():
                    continue
                value, observed = value[:, eligible], observed[:, eligible]
                finite = observed & np.isfinite(value)
                actual = value[finite]
                adjacent = finite[1:] & finite[:-1]
                delta = np.abs(value[1:] - value[:-1])[adjacent]
                rows.append({
                    "modality": modality, "city": city, "metric": metric,
                    "city_basis": "traffic_target_region" if modality == "edge" else "node_id",
                    "eligible_entities": int(np.count_nonzero(eligible)),
                    "observed_cells": int(actual.size),
                    "observed_fraction": rounded(actual.size / finite.size),
                    "zero_fraction_among_observed": rounded(float(np.mean(actual == 0))) if actual.size else None,
                    "value_p50": rounded(quantile(actual, .5)),
                    "value_p95": rounded(quantile(actual, .95)),
                    "value_p99": rounded(quantile(actual, .99)),
                    "adjacent_minute_pairs": int(delta.size),
                    "absolute_delta_p50": rounded(quantile(delta, .5)),
                    "absolute_delta_p95": rounded(quantile(delta, .95)),
                    "absolute_delta_p99": rounded(quantile(delta, .99)),
                    "absolute_delta_p999": rounded(quantile(delta, .999)),
                })
    fields = (
        "modality", "city", "metric", "city_basis", "eligible_entities",
        "observed_cells", "observed_fraction", "zero_fraction_among_observed",
        "value_p50", "value_p95", "value_p99", "adjacent_minute_pairs",
        "absolute_delta_p50", "absolute_delta_p95", "absolute_delta_p99", "absolute_delta_p999",
    )
    write_csv(out / "metric_profile.csv", fields, rows)
    return {"metric_profile_rows": len(rows)}


def dimension_delta_report(store: FeatureStore, out: Path) -> dict:
    groups: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {"series": 0, "series_with_pairs": 0, "cells": 0, "pairs": 0, "p99": []}
    )
    for series in store.iter_dimension_series():
        city = series.node_id.split("-", 1)[0]
        item = groups[(city, series.source, series.metric)]
        item["series"] += 1
        item["cells"] += len(series.values)
        times = np.asarray(series.time_indices)
        values = np.asarray(series.values, dtype=np.float32)
        adjacent = (np.diff(times.astype(np.int64)) == 1) & np.isfinite(values[1:]) & np.isfinite(values[:-1])
        if not adjacent.any():
            continue
        delta = np.abs(np.diff(values))[adjacent]
        item["pairs"] += len(delta)
        item["series_with_pairs"] += 1
        item["p99"].append(float(np.quantile(delta, .99)))
    rows = []
    for (city, source, metric), item in sorted(groups.items()):
        p99 = np.asarray(item["p99"], dtype=np.float32)
        rows.append({
            "city": city, "source": source, "metric": metric,
            "series_count": item["series"], "series_with_adjacent_pairs": item["series_with_pairs"],
            "observed_cells": item["cells"], "adjacent_minute_pairs": item["pairs"],
            "median_series_absolute_delta_p99": rounded(quantile(p99, .5)),
            "p90_series_absolute_delta_p99": rounded(quantile(p99, .9)),
            "max_series_absolute_delta_p99": rounded(float(np.max(p99))) if p99.size else None,
            "constant_series_fraction": rounded(float(np.mean(p99 == 0))) if p99.size else None,
        })
    write_csv(out / "dimension_series_delta.csv", (
        "city", "source", "metric", "series_count", "series_with_adjacent_pairs",
        "observed_cells", "adjacent_minute_pairs", "median_series_absolute_delta_p99",
        "p90_series_absolute_delta_p99", "max_series_absolute_delta_p99",
        "constant_series_fraction",
    ), rows)
    return {"dimension_profile_rows": len(rows), "dimension_series_count": sum(v["series"] for v in groups.values())}


def dimension_identifier_report(store: FeatureStore, out: Path) -> dict:
    identifiers: dict[tuple[str, str, str, str, str], set[str]] = defaultdict(set)
    index = json.loads((store.path / "dimension_series.json").read_text(encoding="utf-8"))
    interfaces: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"roles": set(), "metrics": set()}
    )
    services: dict[tuple[str, str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"sources": set(), "metrics": set()}
    )
    for item in index:
        city = item["node_id"].split("-", 1)[0]
        dimensions = dict(item["dimensions"])
        for key, value in dimensions.items():
            if key == "series_key":
                continue
            identifiers[(item["source"], city, item["node_id"], key, value)].add(item["metric"])
        if item["source"] == "interface" and dimensions.get("interface_id"):
            group = interfaces[(item["node_id"], dimensions["interface_id"])]
            group["metrics"].add(item["metric"])
            if dimensions.get("if_role"):
                group["roles"].add(dimensions["if_role"])
        if item["source"] == "traffic" and dimensions.get("target_domain"):
            key = (
                dimensions.get("flow_type", ""),
                dimensions.get("target_region", ""),
                dimensions["target_domain"],
            )
            group = services[key]
            group["sources"].add(dimensions.get("source_region", ""))
            group["metrics"].add(item["metric"])
    rows = [
        {
            "source": source, "city": city, "node_id": node,
            "dimension_key": key, "dimension_value": value,
            "metric_count": len(metrics),
        }
        for (source, city, node, key, value), metrics in sorted(identifiers.items())
    ]
    write_csv(out / "dimension_identifiers.csv", (
        "source", "city", "node_id", "dimension_key", "dimension_value", "metric_count",
    ), rows)
    netflow_interfaces = {
        (edge.source, edge.target.rsplit(":", 1)[-1])
        for edge in store.entities.edges if edge.relation == "netflow"
    }
    interface_rows = [
        {
            "city": node.split("-", 1)[0], "node_id": node,
            "interface_id": interface, "if_roles": ";".join(sorted(group["roles"])),
            "metric_count": len(group["metrics"]),
            "netflow_edge_present": (node, interface) in netflow_interfaces,
            "verified_peer_node_id": "", "verified_peer_interface_id": "",
            "valid_from": "", "valid_to": "", "mapping_evidence": "",
        }
        for (node, interface), group in sorted(interfaces.items())
    ]
    write_csv(out / "interface_link_mapping_worklist.csv", (
        "city", "node_id", "interface_id", "if_roles", "metric_count",
        "netflow_edge_present", "verified_peer_node_id", "verified_peer_interface_id",
        "valid_from", "valid_to", "mapping_evidence",
    ), interface_rows)
    service_rows = [
        {
            "flow_type": flow, "target_region": region, "target_domain": domain,
            "source_regions": ";".join(sorted(group["sources"])),
            "metric_count": len(group["metrics"]),
            "verified_service_node_id": "", "valid_from": "", "valid_to": "",
            "mapping_evidence": "",
        }
        for (flow, region, domain), group in sorted(services.items())
    ]
    write_csv(out / "service_domain_mapping_worklist.csv", (
        "flow_type", "target_region", "target_domain", "source_regions",
        "metric_count", "verified_service_node_id", "valid_from", "valid_to",
        "mapping_evidence",
    ), service_rows)
    return {
        "dimension_identifier_rows": len(rows),
        "interface_mapping_rows": len(interface_rows),
        "service_domain_mapping_rows": len(service_rows),
    }


def candidate_report(predictions: Path, inference: Path, audit: Path, out: Path) -> dict:
    with predictions.open(encoding="utf-8") as handle:
        preds = {item["prediction_id"]: item for line in handle if (item := json.loads(line))}
    if inference.suffix == ".gz":
        with gzip.open(inference, "rt", encoding="utf-8") as handle:
            log = json.load(handle)
    else:
        log = json.loads(inference.read_text(encoding="utf-8"))
    events = {item["prediction_id"]: item for item in log["events"]}
    evidence = json.loads(audit.read_text(encoding="utf-8"))
    traces = {item["prediction_id"]: item for item in evidence["events"]}
    if not (preds.keys() == events.keys() == traces.keys()):
        raise ValueError("prediction, inference and evidence IDs disagree")
    rows: list[dict] = []
    for identity, prediction in preds.items():
        event = events[identity]["event"]
        trace = traces[identity]
        category = prediction["fault_category"]
        rows.append({
            "prediction_id": identity, "city": event.get("city_id"),
            "start_time": prediction["start_time"], "end_time": prediction["end_time"],
            "duration_minutes": int(event["end_index"]) - int(event["start_index"]) + 1,
            "root_top1": prediction["root_cause_top5"][0]["network_element_id"],
            "root_top5": ";".join(x["network_element_id"] for x in prediction["root_cause_top5"]),
            "major_category": category["major_category"],
            "sub_category": category["sub_category"],
            "driver_kind": trace["peak_driver"]["kind"],
            "driver_score": rounded(trace["peak_driver"]["score"]),
            "anchor_status": trace["classification_anchor"]["status"],
            "anchor_metric": trace["classification_anchor"].get("metric"),
            "root_direct_strength": rounded(trace["root_direct_strength"]),
            "frr_support_peak": rounded(trace["root_frr_support_peak"]),
            "netflow_support_peak": rounded(trace["root_netflow_raw_support_peak"]),
            "review_priority": "high" if trace["classification_anchor"]["status"] == "unsupported"
                or trace["peak_driver"]["kind"] == "ambiguous" else "standard",
        })
    write_csv(out / "candidate_review.csv", (
        "prediction_id", "city", "start_time", "end_time", "duration_minutes",
        "root_top1", "root_top5", "major_category", "sub_category", "driver_kind",
        "driver_score", "anchor_status", "anchor_metric", "root_direct_strength",
        "frr_support_peak", "netflow_support_peak", "review_priority",
    ), rows)
    counts = Counter((row["city"], row["major_category"], row["sub_category"]) for row in rows)
    summary = [
        {"city": city, "major_category": major, "sub_category": sub, "candidate_count": count}
        for (city, major, sub), count in sorted(counts.items())
    ]
    write_csv(out / "candidate_city_category.csv", (
        "city", "major_category", "sub_category", "candidate_count",
    ), summary)
    return {"candidate_count": len(rows), "high_review_priority_count": sum(r["review_priority"] == "high" for r in rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--inference", type=Path)
    parser.add_argument("--audit", type=Path)
    args = parser.parse_args()
    if any((args.predictions, args.inference, args.audit)) and not all((args.predictions, args.inference, args.audit)):
        parser.error("--predictions, --inference and --audit must be supplied together")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    store = FeatureStore.open(args.store)
    summary = {}
    summary.update(file_and_schema_reports(args.raw_root, store.manifest, args.output_dir))
    summary.update(raw_node_alias_report(args.raw_root, store.manifest, args.output_dir))
    summary.update(node_coverage_report(store, args.output_dir))
    summary.update(metric_profile_report(store, args.output_dir))
    summary.update(dimension_delta_report(store, args.output_dir))
    summary.update(dimension_identifier_report(store, args.output_dir))
    if args.predictions:
        summary.update(candidate_report(args.predictions, args.inference, args.audit, args.output_dir))
    summary.update({
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_root": relative(args.raw_root), "feature_store": relative(args.store),
        "feature_manifest_sha256": hashlib.sha256((args.store / "manifest.json").read_bytes()).hexdigest(),
        "parser_quality_status": store.manifest["parser_audit"].get("quality_status"),
        "period_start": store.manifest["start_time"], "period_end": store.manifest["end_time"],
        "minute_count": store.manifest["minute_count"],
        "method": "full feature-store statistics; raw CSV headers; full node CSV alias scan; manifest audit for other source row counts",
    })
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in summary.items() if k in {
        "files", "cities", "total_rows_in_build_audit", "sources_with_header_drift",
        "fully_unobserved_nodes", "candidate_count", "metric_profile_rows",
        "dimension_profile_rows", "dimension_series_count", "dimension_identifier_rows",
        "interface_mapping_rows", "service_domain_mapping_rows",
        "raw_node_alias_rows", "raw_node_rows_scanned",
    }}, ensure_ascii=False))


if __name__ == "__main__":
    main()

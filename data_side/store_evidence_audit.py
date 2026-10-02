"""Audit v3 feature-store observation gaps, counters, and entity relations.

This is an audit of retained observations, not a fault detector or a copy of
the original CSV audit. Missing minutes outside a series' observed span are
not called gaps, and stored traffic rates are not treated as raw counters.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np

from aiops_v3.calibration import store_fingerprint
from aiops_v3.data.feature_store import FeatureStore
from aiops_v3.data.projection import _token


SERIES_COLUMNS = (
    "source", "node_id", "metric", "dimensions", "observed_minutes",
    "first_observed_time", "last_observed_time", "internal_missing_minutes",
    "gap_runs", "max_gap_minutes",
)
COUNTER_COLUMNS = (
    "source", "node_id", "metric", "dimensions", "counter_semantics",
    "observed_minutes", "positive_steps", "unchanged_steps", "decrease_steps",
    "uncompared_gap_pairs", "first_decrease_time", "reset_signal_minutes",
)
RELATION_COLUMNS = (
    "relation_type", "subject_id", "object_id", "observation_source",
    "provenance", "evidence_fields", "supporting_metrics", "interpretation",
)


def _time(store: FeatureStore, index: int) -> str:
    return (store.start_time + timedelta(minutes=index)).isoformat().replace("+00:00", "Z")


def _series_row(store: FeatureStore, source: str, node_id: str, metric: str,
                dimensions: str, indices: np.ndarray) -> dict:
    times = np.asarray(indices, dtype=np.int64)
    gaps = np.diff(times) - 1
    missing = gaps[gaps > 0]
    return {
        "source": source, "node_id": node_id, "metric": metric,
        "dimensions": dimensions, "observed_minutes": int(len(times)),
        "first_observed_time": _time(store, int(times[0])),
        "last_observed_time": _time(store, int(times[-1])),
        "internal_missing_minutes": int(missing.sum()),
        "gap_runs": int(len(missing)),
        "max_gap_minutes": int(missing.max()) if len(missing) else 0,
    }


def _counter_row(store: FeatureStore, source: str, node_id: str, metric: str,
                 dimensions: str, indices: np.ndarray, values: np.ndarray) -> dict | None:
    if metric.endswith(".counter_reset"):
        return {
            "source": source, "node_id": node_id, "metric": metric,
            "dimensions": dimensions, "counter_semantics": "parser_reset_signal",
            "observed_minutes": int(len(indices)), "positive_steps": "",
            "unchanged_steps": "", "decrease_steps": "",
            "uncompared_gap_pairs": "", "first_decrease_time": "",
            "reset_signal_minutes": int(np.count_nonzero(np.asarray(values) > 0)),
        }
    if not metric.endswith("_total"):
        return None
    times = np.asarray(indices, dtype=np.int64)
    data = np.asarray(values, dtype=np.float64)
    adjacent = np.diff(times) == 1
    changes = np.diff(data)[adjacent]
    decreases = np.flatnonzero(adjacent & (np.diff(data) < 0))
    return {
        "source": source, "node_id": node_id, "metric": metric,
        "dimensions": dimensions, "counter_semantics": "raw_cumulative",
        "observed_minutes": int(len(times)),
        "positive_steps": int(np.count_nonzero(changes > 0)),
        "unchanged_steps": int(np.count_nonzero(changes == 0)),
        "decrease_steps": int(len(decreases)),
        "uncompared_gap_pairs": int(np.count_nonzero(~adjacent)),
        "first_decrease_time": _time(store, int(times[decreases[0] + 1])) if len(decreases) else "",
        "reset_signal_minutes": "",
    }


def _add_relation(relations: dict, relation_type: str, subject_id: str,
                  object_id: str, source: str, provenance: str,
                  evidence_fields: str, metric: str, interpretation: str) -> None:
    key = (relation_type, subject_id, object_id, source)
    if key not in relations:
        relations[key] = {
            "relation_type": relation_type, "subject_id": subject_id,
            "object_id": object_id, "observation_source": source,
            "provenance": provenance, "evidence_fields": evidence_fields,
            "supporting_metrics": set(), "interpretation": interpretation,
        }
    if metric:
        relations[key]["supporting_metrics"].add(metric)


def _dimension_relations(relations: dict, source: str, node_id: str,
                         metric: str, dimensions: dict[str, str]) -> None:
    if source == "interface" and dimensions.get("interface_id"):
        _add_relation(relations, "owns_observed_interface", node_id,
                      f"interface:{node_id}:{_token(dimensions['interface_id'])}",
                      source, "dimension_sidecar", "interface_id", metric,
                      "same observation names node and interface; peer is unverified")
    if source == "routing":
        for field, relation_type in (("peer", "references_route_peer"),
                                     ("next_hop", "references_route_next_hop")):
            if dimensions.get(field):
                _add_relation(relations, relation_type, node_id, dimensions[field],
                              source, "dimension_sidecar", field, metric,
                              "routing label references address; address-to-node mapping is unverified")
    if source == "traffic" and dimensions.get("target_domain"):
        domain = dimensions["target_domain"]
        _add_relation(relations, "probes_service_domain", node_id, domain,
                      source, "dimension_sidecar", "source_region|target_domain", metric,
                      "probe target domain is observed; service instance is unverified")
        if dimensions.get("target_region"):
            _add_relation(relations, "service_domain_targets_city", domain,
                          dimensions["target_region"], source, "dimension_sidecar",
                          "target_domain|target_region", metric,
                          "target city is observed; service instance is unverified")


def audit_feature_store(input_store: Path) -> dict:
    """Return tables based on v3 store masks and retained dimension series."""
    input_store = Path(input_store)
    store = FeatureStore.open(input_store)
    if int(store.manifest.get("format_version", 0)) < 2:
        raise ValueError("dimension sidecar is required for provenance audit")
    series_rows: list[dict] = []
    counter_rows: list[dict] = []
    relations: dict[tuple[str, str, str, str], dict] = {}
    dimension_coverage: dict[tuple[str, str], np.ndarray] = {}

    for series in store.iter_dimension_series():
        dimensions = dict(series.dimensions)
        dimensions_text = json.dumps(dimensions, sort_keys=True, ensure_ascii=False)
        key = (series.node_id, series.metric)
        coverage = dimension_coverage.get(key)
        if coverage is None:
            coverage = np.zeros(int(store.manifest["minute_count"]), dtype=bool)
            dimension_coverage[key] = coverage
        coverage[series.time_indices] = True
        if not len(series.time_indices):
            continue
        series_rows.append(_series_row(store, series.source, series.node_id,
                                       series.metric, dimensions_text, series.time_indices))
        counter = _counter_row(store, series.source, series.node_id, series.metric,
                               dimensions_text, series.time_indices, series.values)
        if counter is not None:
            counter_rows.append(counter)
        _dimension_relations(relations, series.source, series.node_id,
                             series.metric, dimensions)

    for feature_index, metric in enumerate(store.features.names("node")):
        source = metric.split(".", 1)[0]
        for node_index, node_id in enumerate(store.entities.nodes):
            indices = np.flatnonzero(store.node_mask[:, node_index, feature_index])
            coverage = dimension_coverage.get((node_id, metric))
            if coverage is not None:
                indices = indices[~coverage[indices]]
            if not len(indices):
                continue
            series_rows.append(_series_row(store, source, node_id, metric, "{}", indices))
            counter = _counter_row(store, source, node_id, metric, "{}", indices,
                                   store.node_values[indices, node_index, feature_index])
            if counter is not None:
                counter_rows.append(counter)

    for edge_index, edge in enumerate(store.entities.edges):
        if edge.relation != "netflow":
            continue
        _add_relation(relations, "observes_netflow_interface", edge.source,
                      edge.target, "netflow", "edge_registry", "node|interface_id", "",
                      "flow observer interface; physical peer is unverified")
        for feature_index, metric in enumerate(store.features.names("edge")):
            if not metric.startswith("netflow."):
                continue
            indices = np.flatnonzero(store.edge_mask[:, edge_index, feature_index])
            if not len(indices):
                continue
            dimensions = json.dumps({"edge_target": edge.target}, ensure_ascii=False)
            series_rows.append(_series_row(store, "netflow", edge.source,
                                           metric, dimensions, indices))
            relations[("observes_netflow_interface", edge.source,
                       edge.target, "netflow")]["supporting_metrics"].add(metric)

    relation_rows = []
    for row in relations.values():
        relation_rows.append({**row,
                              "supporting_metrics": "|".join(sorted(row["supporting_metrics"]))})
    series_rows.sort(key=lambda row: (row["source"], row["node_id"], row["metric"], row["dimensions"]))
    counter_rows.sort(key=lambda row: (row["source"], row["node_id"], row["metric"], row["dimensions"]))
    relation_rows.sort(key=lambda row: (row["relation_type"], row["subject_id"], row["object_id"]))
    source_counts = Counter(row["source"] for row in series_rows)
    relation_counts = Counter(row["relation_type"] for row in relation_rows)
    return {
        "summary": {
            "format_version": 1,
            "input_store": str(input_store),
            "input_manifest_sha256": hashlib.sha256((input_store / "manifest.json").read_bytes()).hexdigest(),
            "input_store_sha256": store_fingerprint(input_store),
            "source_profile": store.manifest.get("source_profile", "stage1_or_unknown"),
            "minute_count": int(store.manifest["minute_count"]),
            "series_count": len(series_rows),
            "series_by_source": dict(sorted(source_counts.items())),
            "series_with_internal_gaps": sum(row["gap_runs"] > 0 for row in series_rows),
            "counter_audit_rows": len(counter_rows),
            "raw_counter_series_count": sum(row["counter_semantics"] == "raw_cumulative"
                                            for row in counter_rows),
            "reset_signal_series_count": sum(row["counter_semantics"] == "parser_reset_signal"
                                             for row in counter_rows),
            "counter_decrease_steps": sum(int(row["decrease_steps"] or 0) for row in counter_rows),
            "parser_reset_signal_cells": sum(int(row["reset_signal_minutes"] or 0)
                                             for row in counter_rows),
            "relation_count": len(relation_rows),
            "relations_by_type": dict(sorted(relation_counts.items())),
            "gap_semantics": "internal_observation_gaps_only",
            "verified_physical_links": 0,
            "limitations": [
                "Feature-store minute aggregation hides duplicate and order information from raw rows.",
                "Raw traffic cumulative values are transformed before storage; only parser reset signals are available.",
                "Counter decreases in stored float32 raw series are candidates for reset or rollover, not confirmed faults.",
                "Relations describe retained observation fields and do not establish physical peers or service instances.",
            ],
        },
        "series": series_rows,
        "counters": counter_rows,
        "relations": relation_rows,
    }


def _write_csv(path: Path, rows: list[dict], columns: tuple[str, ...]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_audit(report: dict, output_dir: Path) -> None:
    """Write a new audit directory without overwriting an existing result."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "summary.json").write_text(
        json.dumps(report["summary"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "series_gaps.csv", report["series"], SERIES_COLUMNS)
    _write_csv(output_dir / "counter_audit.csv", report["counters"], COUNTER_COLUMNS)
    _write_csv(output_dir / "entity_relations.csv", report["relations"], RELATION_COLUMNS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-store", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = audit_feature_store(args.input_store)
    write_audit(report, args.output_dir)
    print(json.dumps({"output_dir": str(args.output_dir),
                      "series_count": report["summary"]["series_count"],
                      "relation_count": report["summary"]["relation_count"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()

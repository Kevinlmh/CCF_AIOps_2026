"""Reconcile the earlier DYX dictionary with the now-complete stage-1 CSVs."""

from __future__ import annotations

import csv
from datetime import timedelta
import json
from pathlib import Path

import numpy as np

from AIOps_data_dyx.pipeline.definitions import FIELD_DEFINITIONS, infer_traffic_field
from aiops_v2.data.feature_store import FeatureStore


BASE = Path(__file__).resolve().parent
REPO = BASE.parent
RESULTS = BASE / "results"
SOURCE_NAMES = {
    "node": "node_metrics", "interface": "interface_metrics",
    "routing": "routing_metrics", "scrape": "scrape_health",
    "traffic": "traffic_flow_metrics", "netflow": "netflow_5tuple",
    "frr": "frr_syslog_events",
}


def read_csv(name: str) -> list[dict]:
    with (RESULTS / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(name: str, fields: tuple[str, ...], rows: list[dict]) -> None:
    with (RESULTS / name).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    schema = read_csv("raw_schema.csv")
    missing = {(r["source"], r["field"]): r for r in read_csv("raw_field_quality_by_source.csv")}
    conditional = {(r["flow_type"], r["field"]): r for r in read_csv("traffic_flow_field_quality_by_flow.csv")}
    store = FeatureStore.open(REPO / "data/feature_store/v2/stage1_all_cities_v2_20260926")
    names = store.features.names("node")
    scale_rows = []
    for field in ("cpu_usage", "disk_io_util", "memory_available_ratio", "swap_used_ratio", "filesystem_used_ratio", "inode_used_ratio", "open_fd_ratio"):
        index = names.index("node." + field)
        values = store.node_values[:, :, index][store.node_mask[:, :, index]]
        scale_rows.append({
            "field": field, "observed_cells": int(values.size),
            "minimum": float(np.min(values)), "maximum": float(np.max(values)),
            "cells_above_one": int(np.count_nonzero(values > 1)),
            "cells_above_100": int(np.count_nonzero(values > 100)),
            "previous_dictionary_unit": FIELD_DEFINITIONS["node_metrics"][field]["unit"],
        })
    disk_index = names.index("node.disk_io_util")
    disk_cells = np.argwhere(
        store.node_mask[:, :, disk_index] & (store.node_values[:, :, disk_index] > 100)
    )
    write_csv("disk_io_above_100.csv", ("timestamp_utc", "node_id", "disk_io_util"), [
        {
            "timestamp_utc": (store.start_time + timedelta(minutes=int(time_index))).isoformat().replace("+00:00", "Z"),
            "node_id": store.entities.nodes[int(node_index)],
            "disk_io_util": float(store.node_values[time_index, node_index, disk_index]),
        }
        for time_index, node_index in disk_cells
    ])
    write_csv("unit_scale_audit.csv", (
        "field", "observed_cells", "minimum", "maximum", "cells_above_one",
        "cells_above_100", "previous_dictionary_unit",
    ), scale_rows)
    rows = []
    for item in schema:
        source, field = item["source"], item["field"]
        definition = (
            infer_traffic_field(field) if source == "traffic"
            else FIELD_DEFINITIONS[SOURCE_NAMES[source]][field]
        )
        if definition.get("type") == "unknown":
            raise ValueError(f"field is not covered by existing dictionary: {source}.{field}")
        unit = definition.get("unit", "metric-specific")
        note = ""
        if source == "node" and field in {"cpu_usage", "disk_io_util"}:
            unit = "scale_unconfirmed; observed values exceed 1"
            note = "Older DYX dictionary calls this a ratio; current values contradict a 0-1 interpretation."
        if source == "netflow" and field == "if_role":
            note = "All 465,667,376 current raw rows have a missing if_role."
        if source == "routing" and field == "value":
            note = "Missingness is conditional on metric_name and device role; see routing_metric_role_quality.csv."
        quality = missing[(source, field)]
        conditional_missing = ""
        if source == "traffic" and "_flow_" in field:
            flow = field.split("_flow_", 1)[0]
            selected = conditional.get((flow, field))
            if selected:
                conditional_missing = selected["conditional_missing_fraction"]
                note = (note + " " if note else "") + "Raw missing fraction includes rows for other flow types."
        rows.append({
            "source": source, "field": field, "type": definition.get("type", ""),
            "unit_or_scale": unit, "meaning": definition.get("meaning", ""),
            "analytical_role": definition.get("role", ""),
            "raw_missing_fraction": quality["missing_fraction"],
            "active_flow_missing_fraction": conditional_missing,
            "interpretation_status": "project_interpretation_not_official",
            "caveat": note,
        })
    write_csv("field_dictionary_reconciled.csv", (
        "source", "field", "type", "unit_or_scale", "meaning", "analytical_role",
        "raw_missing_fraction", "active_flow_missing_fraction",
        "interpretation_status", "caveat",
    ), rows)
    print(json.dumps({"dictionary_fields": len(rows), "scale_audit_fields": len(scale_rows),
                      "disk_io_cells_above_100": len(disk_cells)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

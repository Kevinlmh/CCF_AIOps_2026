"""Explain long-form routing missingness and flow-conditional traffic missingness."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess

from baseline.bian.preprocessing.multisource import iter_source_files
from baseline.bian.preprocessing.observations import city_from_path
from aiops_challenge_2026.config import load_public_config

from scan_raw_quality import BASE, RAW, RESULTS, helper_binary, write_csv


def grouped_profile(source: str, path: Path, group_field: str, binary: Path) -> tuple[str, list[dict]]:
    output = subprocess.run(
        [str(binary), str(path), "--group", group_field],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    fields = [line.split("\t", 1)[0] for line in output if line and line[0] not in "@#"]
    city = city_from_path(path, {city: city for city in load_public_config("network_elements")["cities"]})
    rows = []
    for line in output:
        if not line.startswith("#\t"):
            continue
        parts = line.split("\t")
        count = int(parts[2])
        missing = [int(value) for value in parts[3:]]
        if len(missing) != len(fields):
            raise ValueError(f"grouped scanner field count mismatch: {path}")
        rows.append({
            "city": city, "source": source, "group": parts[1], "rows": count,
            "missing": dict(zip(fields, missing)),
        })
    return source, rows


def main() -> None:
    binary = helper_binary()
    tasks = [
        (source, path, "metric_name" if source == "routing" else "flow_type")
        for source, path in iter_source_files(RAW) if source in {"routing", "traffic"}
    ]
    grouped = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(grouped_profile, *task, binary): task for task in tasks}
        for future in as_completed(futures):
            source, rows = future.result()
            grouped.extend(rows)
            print(f"grouped {source} file: {len(rows)} groups", flush=True)
    routing = []
    traffic = []
    routing_totals = defaultdict(lambda: [0, 0, 0])
    traffic_totals = defaultdict(lambda: [0, 0])
    for item in grouped:
        if item["source"] == "routing":
            value_missing = item["missing"]["value"]
            label_missing = item["missing"]["label"]
            routing.append({
                "city": item["city"], "metric_name": item["group"],
                "rows": item["rows"], "value_missing": value_missing,
                "value_missing_fraction": round(value_missing / item["rows"], 8),
                "label_missing": label_missing,
                "label_missing_fraction": round(label_missing / item["rows"], 8),
            })
            total = routing_totals[item["group"]]
            total[0] += item["rows"]
            total[1] += value_missing
            total[2] += label_missing
        else:
            for field, missing in item["missing"].items():
                if not field.startswith(item["group"] + "_flow_"):
                    continue
                traffic.append({
                    "city": item["city"], "flow_type": item["group"],
                    "field": field, "active_flow_rows": item["rows"],
                    "missing_on_active_flow_rows": missing,
                    "conditional_missing_fraction": round(missing / item["rows"], 8),
                })
                total = traffic_totals[(item["group"], field)]
                total[0] += item["rows"]
                total[1] += missing
    routing.sort(key=lambda x: (x["metric_name"], x["city"]))
    traffic.sort(key=lambda x: (x["flow_type"], x["field"], x["city"]))
    write_csv(RESULTS / "routing_metric_quality.csv", (
        "city", "metric_name", "rows", "value_missing", "value_missing_fraction",
        "label_missing", "label_missing_fraction",
    ), routing)
    write_csv(RESULTS / "traffic_flow_field_quality.csv", (
        "city", "flow_type", "field", "active_flow_rows",
        "missing_on_active_flow_rows", "conditional_missing_fraction",
    ), traffic)
    routing_aggregate = [
        {
            "metric_name": name, "rows": values[0], "value_missing": values[1],
            "value_missing_fraction": round(values[1] / values[0], 8),
            "label_missing": values[2],
            "label_missing_fraction": round(values[2] / values[0], 8),
        }
        for name, values in sorted(routing_totals.items())
    ]
    traffic_aggregate = [
        {
            "flow_type": flow, "field": field, "active_flow_rows": values[0],
            "missing_on_active_flow_rows": values[1],
            "conditional_missing_fraction": round(values[1] / values[0], 8),
        }
        for (flow, field), values in sorted(traffic_totals.items())
    ]
    write_csv(RESULTS / "routing_metric_quality_by_metric.csv", (
        "metric_name", "rows", "value_missing", "value_missing_fraction",
        "label_missing", "label_missing_fraction",
    ), routing_aggregate)
    write_csv(RESULTS / "traffic_flow_field_quality_by_flow.csv", (
        "flow_type", "field", "active_flow_rows",
        "missing_on_active_flow_rows", "conditional_missing_fraction",
    ), traffic_aggregate)
    role_rows = []
    role_tasks = [(source, path, "metric_name+node_type") for source, path in iter_source_files(RAW) if source == "routing"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(grouped_profile, *task, binary) for task in role_tasks]
        for future in as_completed(futures):
            _, rows = future.result()
            for item in rows:
                metric, role = item["group"].split("|", 1)
                role_rows.append({
                    "city": item["city"], "metric_name": metric, "node_type": role,
                    "rows": item["rows"], "value_missing": item["missing"]["value"],
                    "value_missing_fraction": round(item["missing"]["value"] / item["rows"], 8),
                })
    role_rows.sort(key=lambda x: (x["metric_name"], x["node_type"], x["city"]))
    write_csv(RESULTS / "routing_metric_role_quality.csv", (
        "city", "metric_name", "node_type", "rows", "value_missing",
        "value_missing_fraction",
    ), role_rows)
    print(json.dumps({
        "routing_city_metric_rows": len(routing), "routing_metrics": len(routing_aggregate),
        "routing_city_metric_role_rows": len(role_rows),
        "traffic_city_flow_field_rows": len(traffic),
        "traffic_flow_fields": len(traffic_aggregate),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

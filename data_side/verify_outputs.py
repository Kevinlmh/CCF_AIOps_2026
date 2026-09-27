"""Check that generated data-side reports reconcile with each other."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
import json
from pathlib import Path


RESULTS = Path(__file__).resolve().parent / "results"


def rows(name: str) -> list[dict[str, str]]:
    with (RESULTS / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    summary = json.loads((RESULTS / "raw_rescan_summary.json").read_text(encoding="utf-8"))
    report = json.loads((RESULTS / "summary.json").read_text(encoding="utf-8"))
    files = rows("raw_file_rescan.csv")
    fields = rows("raw_field_quality.csv")
    aggregate = rows("raw_field_quality_by_source.csv")
    schema = rows("raw_schema.csv")
    dictionary = rows("field_dictionary_reconciled.csv")
    assert len(files) == summary["files"] == report["files"] == 56
    assert sum(int(item["rows"]) for item in files) == summary["rows"] == report["total_rows_in_build_audit"]
    assert summary["malformed_records"] == 0
    assert len(fields) == summary["field_rows"] == 1656
    assert len(aggregate) == len(schema) == len(dictionary) == 207
    keys = {(item["source"], item["field"]) for item in schema}
    assert keys == {(item["source"], item["field"]) for item in aggregate}
    assert keys == {(item["source"], item["field"]) for item in dictionary}

    by_field = defaultdict(lambda: [0, 0])
    for item in fields:
        key = item["source"], item["field"]
        by_field[key][0] += int(item["rows"])
        by_field[key][1] += int(item["missing"])
    for item in aggregate:
        assert by_field[item["source"], item["field"]] == [int(item["rows"]), int(item["missing"])]

    partial = rows("node_partial_missing_rows.csv")
    disk_above_100 = rows("disk_io_above_100.csv")
    assert len(disk_above_100) == 47
    assert all(float(item["disk_io_util"]) > 100 for item in disk_above_100)
    node_missing = Counter()
    for item in partial:
        for field in item["missing_fields"].split("|"):
            node_missing[field] += 1
    assert len(partial) == 22
    for item in aggregate:
        if item["source"] == "node" and item["field"] in node_missing:
            assert int(item["missing"]) == node_missing[item["field"]]

    routing_metric = rows("routing_metric_quality_by_metric.csv")
    assert sum(int(item["rows"]) for item in routing_metric) == summary["source_rows"]["routing"]
    routing_roles = rows("routing_metric_role_quality.csv")
    assert sum(int(item["rows"]) for item in routing_roles) == summary["source_rows"]["routing"]
    traffic = rows("traffic_flow_field_quality_by_flow.csv")
    flow_rows = {
        item["flow_type"]: int(item["active_flow_rows"])
        for item in traffic if item["field"] == item["flow_type"] + "_flow_requests_total"
    }
    assert sum(flow_rows.values()) == summary["source_rows"]["traffic"]

    cases = json.loads((RESULTS / "public_case_evidence.json").read_text(encoding="utf-8"))
    assert len(cases) == 3
    assert all(item["root_primary_change_rank_within_city"] == 1 for item in cases)
    assert len(rows("candidate_review.csv")) == report["candidate_count"]
    comparison = json.loads((RESULTS / "threshold_comparison.json").read_text(encoding="utf-8"))
    assert comparison["current"]["candidate_count"] == report["candidate_count"]
    print(json.dumps({
        "files": len(files), "rows": summary["rows"], "dictionary_fields": len(dictionary),
        "node_partial_missing_rows": len(partial), "disk_io_cells_above_100": len(disk_above_100),
        "public_cases": len(cases), "candidate_events": report["candidate_count"],
        "status": "pass",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

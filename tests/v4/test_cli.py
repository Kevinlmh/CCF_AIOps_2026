"""Subprocess checks for public CLI behavior and output protection."""

import csv
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_records import write_csv


REPOSITORY = Path(__file__).resolve().parents[2]


def run(*arguments):
    return subprocess.run([sys.executable, "-m", "aiops_v4.data", *map(str, arguments)],
                          cwd=REPOSITORY, capture_output=True, text=True)


def fixture(root):
    write_csv(root, "node_metrics.csv", [
        {"timestamp": "2026-09-17T04:00:00Z", "node": "br1", "cpu_usage": "10"},
        {"timestamp": "2026-09-17T04:01:00Z", "node": "br2", "cpu_usage": "20"},
    ])


def test_profile_writes_scope_quality_dictionary_and_rejects_repeated_output(tmp_path):
    root, report = tmp_path / "input", tmp_path / "report"
    fixture(root)
    arguments = ("profile", "--root", root, "--batch", "stage2", "--report-dir", report,
                 "--max-rows-per-file", "1", "--verbose")
    result = run(*arguments)
    assert result.returncode == 0, result.stderr
    profile = json.loads((report / "profile.json").read_text())
    assert profile["mode"] == "prefix_sample" and profile["rows_scanned"] == 1
    assert not profile["complete"]
    assert "prefix_sample" in (report / "report.md").read_text()
    assert "node.cpu_usage" in (report / "fields.csv").read_text()
    with (report / "fields.csv").open(newline="") as stream:
        fields = {row["field"]: row for row in csv.DictReader(stream)}
    assert fields["node.cpu_usage"]["numeric_count"] == "1"
    assert fields["node.cpu_usage"]["missing_count"] == "0"
    assert fields["node.cpu_usage"]["quantile_sample_count"] == "1"
    assert json.loads(result.stdout)["rows_scanned"] == 1
    assert "file_done" in result.stderr
    before = (report / "profile.json").read_bytes()
    repeated = run(*arguments)
    assert repeated.returncode == 2 and "already exists" in repeated.stderr
    assert (report / "profile.json").read_bytes() == before


def test_ingest_and_query_cli_round_trip_original_evidence(tmp_path):
    root, report, database = tmp_path / "input", tmp_path / "report", tmp_path / "evidence.sqlite"
    fixture(root)
    created = run("ingest", "--root", root, "--batch", "stage2", "--database", database,
                  "--report-dir", report)
    assert created.returncode == 0, created.stderr
    queried = run("query", "--database", database, "--batch", "stage2", "--node-id", "xian-br-2")
    assert queried.returncode == 0, queried.stderr
    rows = [json.loads(line) for line in queried.stdout.splitlines()]
    assert len(rows) == 1 and rows[0]["raw"]["cpu_usage"] == "20"
    assert rows[0]["line_start"] == 3
    empty = run("query", "--database", database, "--batch", "stage1")
    assert empty.returncode == 0 and empty.stdout == ""


def test_ingest_checks_existing_report_before_creating_database(tmp_path):
    root, report, database = tmp_path / "input", tmp_path / "report", tmp_path / "evidence.sqlite"
    fixture(root)
    report.mkdir()
    (report / "keep.txt").write_text("keep")
    result = run("ingest", "--root", root, "--batch", "stage2", "--database", database,
                 "--report-dir", report)
    assert result.returncode == 2 and "already exists" in result.stderr
    assert not database.exists() and (report / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("arguments", [
    ("profile", "--max-rows-per-file", "0"),
    ("query", "--limit", "0"),
    ("query", "--start", "2026-09-17T04:00:00"),
])
def test_invalid_cli_arguments_return_error_without_traceback(tmp_path, arguments):
    root, report, database = tmp_path / "input", tmp_path / "report", tmp_path / "evidence.sqlite"
    fixture(root)
    if arguments[0] == "profile":
        result = run(*arguments, "--root", root, "--batch", "stage2", "--report-dir", report)
    else:
        result = run(*arguments, "--database", database, "--batch", "stage2")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert not report.exists() and not database.exists()


def test_profile_errors_publish_visible_partial_report_and_fail_command(tmp_path):
    root, report = tmp_path / "input", tmp_path / "report"
    root.mkdir()
    (root / "node_metrics.csv").write_text("timestamp,node,node\na,b,c\n")
    result = run("profile", "--root", root, "--batch", "stage2", "--report-dir", report)
    assert result.returncode == 2
    profile = json.loads((report / "profile.json").read_text())
    assert profile["errors"] and not profile["complete"]
    assert "duplicate" in (report / "report.md").read_text()


@pytest.mark.parametrize("destination", ["report", "database"])
def test_outputs_cannot_be_placed_inside_scanned_input(tmp_path, destination):
    root = tmp_path / "input"
    fixture(root)
    report = root / "report" if destination == "report" else tmp_path / "report"
    database = root / "evidence.sqlite" if destination == "database" else tmp_path / "evidence.sqlite"
    result = run("ingest", "--root", root, "--batch", "stage2", "--database", database,
                 "--report-dir", report)
    assert result.returncode == 2 and "input" in result.stderr
    assert not report.exists() and not database.exists()


def test_routing_dictionary_exports_per_metric_distributions(tmp_path):
    root, report = tmp_path / "input", tmp_path / "report"
    write_csv(root, "routing_metrics.csv", [
        {"timestamp": "2026-09-17T04:00:00Z", "node": "br1", "metric_name": "bgp_peer_up", "value": "0"},
        {"timestamp": "2026-09-17T04:00:00Z", "node": "br1", "metric_name": "route_count", "value": "1000"},
    ])
    result = run("profile", "--root", root, "--batch", "stage2", "--report-dir", report)
    assert result.returncode == 0, result.stderr
    with (report / "fields.csv").open(newline="") as stream:
        fields = {row["field"]: row for row in csv.DictReader(stream)}
    assert fields["routing.value"]["mean"] == ""
    assert fields['routing.value["bgp_peer_up"]']["mean"] == "0.0"
    assert fields['routing.value["route_count"]']["mean"] == "1000.0"

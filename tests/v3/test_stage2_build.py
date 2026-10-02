"""The five-source stage-2 layout uses the canonical v3 projector."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from aiops_v3.data import build as build_module
from aiops_v3.data.build import build_raw_feature_store
from aiops_v3.run import run
from aiops_v3.store import open_store


CITIES = ("beida", "chengdu", "guangzhou", "nanjing", "shanghai", "shenyang", "wuhan", "xian")
WINDOWS = (
    ("20260917040000", "20260919040000"),
    ("20260919040000", "20260921040000"),
    ("20260921040000", "20260924040000"),
)


def _write(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def _stage2(root: Path) -> None:
    for city in CITIES:
        for start, end in WINDOWS:
            folder = root / f"{city}_{start}_{end}" / city
            timestamp = datetime.strptime(start, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()
            _write(folder / f"node_metrics_{start}_{end}.csv", {
                "timestamp": timestamp, "node": "br-1", "cpu_usage": "20",
            })
            _write(folder / f"interface_metrics_{start}_{end}.csv", {
                "timestamp": timestamp, "node": "br-1", "interface_id": "eth0", "rx_error_rate": "0",
            })
            _write(folder / f"routing_metrics_{start}_{end}.csv", {
                "timestamp": timestamp, "node": "br-1", "metric_name": "bgp_peer_up",
                "label": 'peer="neighbor"', "value": "1",
            })
            _write(folder / f"scrape_health_{start}_{end}.csv", {
                "timestamp": timestamp, "node": "br-1", "target_id": "x",
                "exporter_type": "node", "scrape_up": "1",
            })
            _write(folder / "netflow_5tuple_minute_readable.csv", {
                "minute_utc": timestamp, "node_key": "br1", "interface_id": "eth0",
                "protocol": "6", "packets": "1", "bytes": "100",
            })


def test_stage2_profile_builds_five_source_shards(tmp_path: Path) -> None:
    raw = tmp_path / "stage2" / "regions"
    _stage2(raw)
    path = build_raw_feature_store(raw, tmp_path / "store", profile="stage2")
    store = open_store(path)
    manifest = store.manifest
    assert manifest["source_profile"] == "stage2"
    assert manifest["expected_sources"] == ["node", "interface", "routing", "scrape", "netflow"]
    assert len(manifest["source_files"]) == 120
    assert manifest["parser_audit"]["quality_status"] == "clean"
    assert manifest["parser_audit"]["files_by_source"] == {
        "node": 24, "interface": 24, "routing": 24, "scrape": 24, "netflow": 24,
    }
    assert manifest["shapes"]["log"][2] == 0
    assert not any(name.startswith("traffic.") for name in manifest["features"]["edge"])
    inference = run(path, tmp_path / "predictions", detector_profile="conservative")
    assert inference.event_count >= 0
    assert (inference.output_dir / "predictions.jsonl").exists()
    run_manifest = json.loads((inference.output_dir / "run_manifest.json").read_text())
    assert run_manifest["source_profile"] == "stage2"


def test_stage2_profile_rejects_missing_city_before_build(tmp_path: Path) -> None:
    raw = tmp_path / "stage2" / "regions"
    _stage2(raw)
    for folder in raw.glob("xian_*"):
        shutil.rmtree(folder)
    with pytest.raises(ValueError, match="missing stage2 bundle"):
        build_raw_feature_store(raw, tmp_path / "store", profile="stage2")
    assert not (tmp_path / "store").exists()


def test_stage2_profile_rejects_duplicate_source_before_build(tmp_path: Path) -> None:
    raw = tmp_path / "stage2" / "regions"
    _stage2(raw)
    folder = raw / "beida_20260917040000_20260919040000" / "beida"
    original = folder / "node_metrics_20260917040000_20260919040000.csv"
    shutil.copyfile(original, folder / "node_metrics_copy.csv")
    with pytest.raises(ValueError, match="duplicate source"):
        build_raw_feature_store(raw, tmp_path / "store", profile="stage2")
    assert not (tmp_path / "store").exists()


def test_stage2_profile_rejects_unexpected_source_before_build(tmp_path: Path) -> None:
    raw = tmp_path / "stage2" / "regions"
    _stage2(raw)
    folder = raw / "beida_20260917040000_20260919040000" / "beida"
    _write(folder / "traffic_flow_metrics.csv", {"timestamp_utc": "2026-09-17T04:00:00Z"})
    with pytest.raises(ValueError, match="unexpected source"):
        build_raw_feature_store(raw, tmp_path / "store", profile="stage2")
    assert not (tmp_path / "store").exists()


def test_stage2_profile_rejects_bad_header_before_build(tmp_path: Path) -> None:
    raw = tmp_path / "stage2" / "regions"
    _stage2(raw)
    folder = raw / "beida_20260917040000_20260919040000" / "beida"
    _write(folder / "node_metrics_20260917040000_20260919040000.csv", {"wrong": "value"})
    with pytest.raises(ValueError, match="missing required column"):
        build_raw_feature_store(raw, tmp_path / "store", profile="stage2")
    assert not (tmp_path / "store").exists()


def test_stage2_preflight_reports_complete_inventory(tmp_path: Path) -> None:
    raw = tmp_path / "stage2" / "regions"
    _stage2(raw)
    report = build_module.inspect_raw_sources(raw, profile="stage2")
    assert report["bundle_count"] == 24
    assert report["file_count"] == 120
    assert report["source_file_counts"] == {
        "node": 24, "interface": 24, "routing": 24, "scrape": 24, "netflow": 24,
    }
    assert report["raw_bytes"] > 0


def test_stage2_preflight_rejects_header_without_building(tmp_path: Path) -> None:
    raw = tmp_path / "stage2" / "regions"
    _stage2(raw)
    folder = raw / "xian_20260921040000_20260924040000" / "xian"
    _write(folder / "scrape_health_20260921040000_20260924040000.csv", {"wrong": "value"})
    with pytest.raises(ValueError, match="missing required column"):
        build_module.inspect_raw_sources(raw, profile="stage2")


def test_stage2_cli_preflight_does_not_create_store(tmp_path: Path) -> None:
    raw = tmp_path / "stage2" / "regions"
    _stage2(raw)
    result = subprocess.run(
        [sys.executable, "-m", "aiops_v3.build_features", "--raw-root", str(raw),
         "--profile", "stage2", "--preflight-only"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["file_count"] == 120
    assert not (tmp_path / "store").exists()


def test_stage2_run_cli_builds_then_detects(tmp_path: Path) -> None:
    raw = tmp_path / "stage2" / "regions"
    _stage2(raw)
    result = subprocess.run(
        [sys.executable, "-m", "aiops_v3.run", "--raw-root", str(raw),
         "--source-profile", "stage2", "--build-store-to", str(tmp_path / "store"),
         "--output-dir", str(tmp_path / "result"), "--detector-profile", "conservative"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "store" / "manifest.json").read_text())["source_profile"] == "stage2"
    assert json.loads((tmp_path / "result" / "run_manifest.json").read_text())["source_profile"] == "stage2"

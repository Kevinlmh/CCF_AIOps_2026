import json
from pathlib import Path

import numpy as np
import pytest

from aiops_v3.contracts import load_contract, validate_prediction
from aiops_v3.detection import detect
from aiops_v3.diagnosis import build_evidence
from aiops_v3.run import run
from aiops_v3.store import open_store


NODES = ["beida-service-vm-1", "beida-service-vm-2", "beida-service-vm-3",
         "beida-fw", "beida-cr-1", "beida-br-1", "beida-br-2",
         "xian-br-1", "xian-br-2", "xian-cr-1", "xian-cr-2", "xian-fw",
         "xian-service-vm-1"]


def fixture_store(tmp_path, cpu):
    tmp_path.mkdir(parents=True, exist_ok=True)
    count = len(cpu)
    values = np.array(cpu, np.float32)[:, None, None]
    mask = np.ones_like(values, bool)
    manifest = {
        "start_time": "2026-09-17T04:00:00Z", "minute_count": count,
        "entities": {"nodes": NODES, "edges": []},
        "features": {"node": ["node.cpu_usage"], "edge": [], "log": []},
        "shapes": {"node": [count, len(NODES), 1], "edge": [count, 0, 0],
                   "log": [count, len(NODES), 0]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    wide = np.repeat(values, len(NODES), axis=1)
    for kind, array, mask_array in (
        ("node", wide, np.ones_like(wide, bool)),
        ("edge", np.zeros((count, 0, 0)), np.zeros((count, 0, 0), bool)),
        ("log", np.zeros((count, len(NODES), 0)), np.zeros((count, len(NODES), 0), bool)),
    ):
        np.save(tmp_path / f"{kind}_values.npy", array)
        np.save(tmp_path / f"{kind}_mask.npy", mask_array)
    return open_store(tmp_path)


def write_stats(path, minutes, *, service="dns", city="beida", req=500, resp=500):
    rows = []
    for minute in minutes:
        for observer in ("beida", "xian"):
            rows.append({"minute": minute, "service": service,
                         "service_node": "beida-service-vm-1",
                         "observer_city": observer,
                         "req_pkts": req, "resp_pkts": resp,
                         "req_bytes": req * 100, "resp_bytes": resp * 100})
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_netflow_service_event_reaches_prediction(tmp_path):
    cpu = [10] * 5 + [10] * 6 + [10] * 20
    store = fixture_store(tmp_path / "store", cpu)
    stats = tmp_path / "stats.jsonl"
    write_stats(stats, range(10, 16), resp=0)
    output = tmp_path / "out"
    run(tmp_path / "store", output, detector_profile="conservative",
        netflow_stats=stats, netflow_events=True)
    rows = [json.loads(line) for line in (output / "predictions.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    validate_prediction(row, load_contract())
    assert row["fault_category"] == {"major_category": "service", "sub_category": "dns_down"}
    assert row["root_cause_top5"][0]["network_element_id"] == "beida-service-vm-1"
    evidence = json.loads((output / "evidence.jsonl").read_text().splitlines()[0])
    assert evidence["observers"] and evidence["quality"]["netflow_observers"] >= 2
    assert evidence["evidence_owners"]
    manifest = json.loads((output / "run_manifest.json").read_text())
    assert manifest["netflow_events"] is True
    assert manifest["netflow_stats_sha256"]


def test_netflow_event_merges_into_overlapping_rule_event(tmp_path):
    cpu = [10] * 10 + [90] * 6 + [10] * 20
    store = fixture_store(tmp_path / "store", cpu)
    stats = tmp_path / "stats.jsonl"
    write_stats(stats, range(10, 16), resp=0)
    output = tmp_path / "out"
    run(tmp_path / "store", output, detector_profile="conservative",
        netflow_stats=stats, netflow_events=True)
    rows = [json.loads(line) for line in (output / "predictions.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    evidence = json.loads((output / "evidence.jsonl").read_text().splitlines()[0])
    features = {signal["feature"] for signal in evidence["signals"]}
    assert "netflow.dns.response_ratio" in features
    assert "node.cpu_usage" in features
    manifest = json.loads((output / "run_manifest.json").read_text())
    assert manifest["merged_events"]


def test_without_netflow_flag_behaviour_is_unchanged(tmp_path):
    cpu = [10] * 5 + [90] * 3 + [10] * 6
    fixture_store(tmp_path / "store", cpu)
    output = tmp_path / "out"
    run(tmp_path / "store", output, detector_profile="conservative")
    rows = [json.loads(line) for line in (output / "predictions.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    evidence = json.loads((output / "evidence.jsonl").read_text().splitlines()[0])
    assert "observers" not in evidence
    features = {signal["feature"] for signal in evidence["signals"]}
    assert "netflow.dns.response_ratio" not in features

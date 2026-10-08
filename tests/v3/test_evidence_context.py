import json
from dataclasses import asdict
from datetime import datetime, timezone

import numpy as np

from aiops_v3.contracts import load_contract
from aiops_v3.data.feature_store import DimensionSeries
from aiops_v3.detection import Event, Signal
from aiops_v3.diagnosis import build_evidence, diagnose_rules
from aiops_v3.server_llm import _pack
from aiops_v3.store import open_store


def _store(path):
    features = ["node.cpu_usage", "node.process_count", "node.memory_available_ratio", "scrape.scrape_up"]
    data = np.zeros((20, 1, 4), np.float32)
    data[:, 0, :] = [10, 40, .8, 1]
    data[5:8, 0, 0] = 80
    mask = np.ones_like(data, bool)
    mask[6, 0, 2] = False
    manifest = {"start_time": "2026-09-17T04:00:00Z", "minute_count": 20,
                "entities": {"nodes": ["xian-service-vm-1"], "edges": []},
                "features": {"node": features, "edge": [], "log": []},
                "shapes": {"node": [20, 1, 4], "edge": [20, 0, 0], "log": [20, 1, 0]},
                "source_counts": {"node": 20, "scrape": 20}}
    (path / "manifest.json").write_text(json.dumps(manifest))
    for kind, values, observed in (("node", data, mask),
                                 ("edge", np.zeros((20, 0, 0)), np.zeros((20, 0, 0), bool)),
                                 ("log", np.zeros((20, 1, 0)), np.zeros((20, 1, 0), bool))):
        np.save(path / f"{kind}_values.npy", values)
        np.save(path / f"{kind}_mask.npy", observed)
    text = {"timestamp": "2026-09-17T04:06:00Z", "node_id": "xian-service-vm-1",
            "source": "scrape", "message": "connection refused", "event_family": "scrape"}
    (path / "text_evidence.jsonl").write_text(json.dumps(text) + "\n")
    return open_store(path)


def _event():
    return Event("v3-e000001", 5, 8, (Signal(
        "node:5:0:node.cpu_usage", 5, "xian-service-vm-1", "node.cpu_usage",
        "cpu_pressure", 80, 10, 17.5, "direct"),))


def test_enriched_evidence_retains_normal_observations_gaps_and_text(tmp_path):
    store = _store(tmp_path)
    ordinary = build_evidence(store, _event(), load_contract())
    rich = build_evidence(store, _event(), load_contract(), include_diagnostic_context=True)
    assert diagnose_rules(rich, load_contract()) == diagnose_rules(ordinary, load_contract())
    card = next(c for c in rich.device_context if c["node"] == "xian-service-vm-1")
    metrics = {m["feature"]: m for m in card["metrics"]}
    assert metrics["node.cpu_usage"]["onset_minute"] == 5
    assert metrics["node.cpu_usage"]["recovery_minute"] == 8
    assert metrics["node.cpu_usage"]["supported_category"] == "cpu_pressure"
    assert metrics["node.process_count"]["supported_category"] is None
    assert metrics["node.process_count"]["during"]["median"] == 40
    assert metrics["node.memory_available_ratio"]["during"]["observed"] == 2
    assert rich.text_evidence[0]["message"] == "connection refused"
    assert rich.available_sources == ("node", "scrape")
    assert rich.sha256() != ordinary.sha256()
    assert _pack(asdict(rich)).sha256() == rich.sha256()
    assert _pack(asdict(ordinary)).sha256() == ordinary.sha256()
    unknown = next(c for c in rich.device_context if c["node"] == "xian-br-1")
    assert unknown["metrics"] == []


def test_dimension_context_tracks_peer_changes_instead_of_device_aggregation():
    class Store:
        manifest = {"minute_count": 20, "source_counts": {"routing": 20}}
        def time_at(self, minute):
            return datetime(2026, 9, 17, 4, minute, tzinfo=timezone.utc)
        def iter_dimension_series(self, *, source=None, node_id=None):
            yield DimensionSeries("routing", "xian-br-1", "routing.bgp_peer_up",
                                  (("peer", "peer-1"),), "low", "min", np.arange(20),
                                  np.array([1] * 5 + [0] * 3 + [1] * 12))

    event = Event("v3-e000002", 5, 8, (Signal(
        "dimension:5:xian-br-1:bgp_peer_up:peer-1", 5, "xian-br-1",
        "routing.bgp_peer_up", "bgp_session_down", 0, 1, 5, "direct"),))
    pack = build_evidence(Store(), event, load_contract(), include_diagnostic_context=True)
    metrics = next(c["metrics"] for c in pack.device_context if c["node"] == "xian-br-1")
    peer = next(m for m in metrics if m.get("dimensions"))
    assert peer["dimensions"] == {"peer": "peer-1"}
    assert (peer["onset_minute"], peer["recovery_minute"]) == (5, 8)


def test_run_records_enriched_evidence_without_changing_predictions(tmp_path):
    from aiops_v3.run import run
    source = tmp_path / "store"
    source.mkdir()
    _store(source)
    run(source, tmp_path / "ordinary")
    run(source, tmp_path / "rich", diagnostic_context=True)
    assert (tmp_path / "ordinary/predictions.jsonl").read_bytes() == (tmp_path / "rich/predictions.jsonl").read_bytes()
    row = json.loads((tmp_path / "rich/evidence.jsonl").read_text())
    assert row["text_evidence"]
    assert _pack(row).sha256() == row["evidence_sha256"]

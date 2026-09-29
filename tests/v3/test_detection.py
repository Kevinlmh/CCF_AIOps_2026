import json

import numpy as np

from aiops_v3.detection import DetectorSettings, detect
from aiops_v3.store import open_store


def fixture_store(tmp_path, cpu, mask=None):
    values = np.array(cpu, np.float32)[:, None, None]
    observed = np.ones_like(values, bool) if mask is None else np.array(mask, bool)[:, None, None]
    count = len(cpu)
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": ["xian-service-vm-1"], "edges": []},
        "features": {"node": ["node.cpu_usage"], "edge": [], "log": []},
        "shapes": {"node": [count, 1, 1], "edge": [count, 0, 0], "log": [count, 1, 0]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for kind, a, m in (("node", values, observed), ("edge", np.zeros((count, 0, 0)), np.zeros((count, 0, 0), bool)), ("log", np.zeros((count, 1, 0)), np.zeros((count, 1, 0), bool))):
        np.save(tmp_path / f"{kind}_values.npy", a)
        np.save(tmp_path / f"{kind}_mask.npy", m)
    return open_store(tmp_path)


def test_sustained_cpu_jump_creates_traceable_event(tmp_path):
    store = fixture_store(tmp_path, [10] * 5 + [78] * 3 + [10] * 6)
    events = detect(store, DetectorSettings())
    assert len(events) == 1
    assert (events[0].start_minute, events[0].end_minute) == (5, 8)
    assert events[0].signals[0].node == "xian-service-vm-1"
    assert events[0].signals[0].feature == "node.cpu_usage"


def test_unobserved_high_value_never_opens_event(tmp_path):
    mask = [True] * 5 + [False] * 3 + [True] * 6
    store = fixture_store(tmp_path, [10] * 5 + [99] * 3 + [10] * 6, mask)
    assert detect(store, DetectorSettings()) == []


def test_subsecond_service_latency_without_direct_fault_does_not_open_event(tmp_path):
    count = 14
    manifest = {
        "start_time": "2026-07-28T12:00:00Z", "minute_count": count,
        "entities": {"nodes": [], "edges": [{"source": "xian-traffic-vm", "target": "service-group:wuhan:auth", "relation": "traffic"}]},
        "features": {"node": [], "edge": ["traffic.auth.latency_p95_seconds"], "log": []},
        "shapes": {"node": [count, 0, 0], "edge": [count, 1, 1], "log": [count, 0, 0]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for kind, values in (
        ("node", np.zeros((count, 0, 0))),
        ("edge", np.array([.05] * 5 + [.5] * 3 + [.05] * 6)[:, None, None]),
        ("log", np.zeros((count, 0, 0))),
    ):
        np.save(tmp_path / f"{kind}_values.npy", values)
        np.save(tmp_path / f"{kind}_mask.npy", np.ones_like(values, bool))
    assert detect(open_store(tmp_path), DetectorSettings()) == []

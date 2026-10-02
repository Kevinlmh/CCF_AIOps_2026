import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from aiops_v3.store import StoreError, open_store
from aiops_v3.data.feature_store import build_feature_store
from aiops_v3.data.observations import NumericObservation


def make_store(tmp_path):
    manifest = {
        "format_version": 4,
        "start_time": "2026-07-28T12:00:00Z",
        "minute_count": 2,
        "entities": {"nodes": ["xian-br-1"], "edges": []},
        "features": {"node": ["node.cpu_usage"], "edge": [], "log": []},
        "shapes": {"node": [2, 1, 1], "edge": [2, 0, 0], "log": [2, 1, 0]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for kind in ("node", "edge", "log"):
        shape = manifest["shapes"][kind]
        np.save(tmp_path / f"{kind}_values.npy", np.full(shape, 99, np.float32))
        np.save(tmp_path / f"{kind}_mask.npy", np.zeros(shape, bool))
    return manifest


def test_store_exposes_only_observed_values(tmp_path):
    make_store(tmp_path)
    store = open_store(tmp_path)
    assert store.node_values.shape == (2, 1, 1)
    assert store.observed_node(0, 0, "node.cpu_usage") is None
    assert isinstance(store.node_values, np.memmap)


def test_store_rejects_manifest_shape_mismatch(tmp_path):
    manifest = make_store(tmp_path)
    manifest["shapes"]["node"] = [3, 1, 1]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(StoreError, match="shape"):
        open_store(tmp_path)


def test_store_rejects_unknown_format_version(tmp_path):
    manifest = make_store(tmp_path)
    manifest["format_version"] = 99
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(StoreError, match="format version"):
        open_store(tmp_path)


def test_store_rejects_non_boolean_mask(tmp_path):
    make_store(tmp_path)
    np.save(tmp_path / "node_mask.npy", np.zeros((2, 1, 1), np.float32))
    with pytest.raises(StoreError, match="dtype"):
        open_store(tmp_path)


def test_inference_store_exposes_canonical_peer_dimension_series(tmp_path):
    t0 = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
    observations = [NumericObservation(
        timestamp=t0 + timedelta(minutes=minute), source="routing", node_id="xian-br-1",
        related_node_ids=(), metric="routing.bgp_peer_prefix_received", value=value,
        dimensions=(("peer", "p1"),), direction="both",
    ) for minute, value in ((0, 4), (1, 1))]
    network = json.loads((Path(__file__).parents[2] / "aiops_v3/config/network_elements.json").read_text())
    build_feature_store(observations, network, tmp_path / "canonical")
    store = open_store(tmp_path / "canonical")
    rows = list(store.iter_dimension_series(source="routing", node_id="xian-br-1"))
    assert len(rows) == 1
    assert list(rows[0].values) == [4, 1]

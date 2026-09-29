import json

import numpy as np
import pytest

from aiops_v3.store import StoreError, open_store


def make_store(tmp_path):
    manifest = {
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

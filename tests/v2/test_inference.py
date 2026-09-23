from __future__ import annotations

from datetime import datetime, timedelta, timezone

import torch

from aiops_challenge_2026.config import load_public_config
from aiops_v2.data.feature_store import build_feature_store
from aiops_v2.data.windows import WindowDataset
from aiops_v2.models.detector import ModelDimensions, MultiSourceDetector
from aiops_v2.training.inference import score_timeline
from baseline.bian.preprocessing.observations import NumericObservation


START = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)


def _cpu(minute: int, value: float) -> NumericObservation:
    return NumericObservation(
        timestamp=START + timedelta(minutes=minute),
        source="node",
        node_id="beida-br-1",
        related_node_ids=(),
        metric="node.cpu_usage",
        value=value,
        dimensions=(),
        direction="high",
    )


def test_score_timeline_merges_overlapping_windows_to_original_length(tmp_path) -> None:
    store = build_feature_store(
        [_cpu(0, 1.0), _cpu(1, 2.0), _cpu(2, 4.0)],
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    dataset = WindowDataset(store, window_minutes=2, stride_minutes=1)
    model = MultiSourceDetector(
        ModelDimensions(node_features=1, edge_features=0, log_features=0, hidden_size=8)
    )
    for parameter in model.parameters():
        torch.nn.init.zeros_(parameter)

    scores = score_timeline(model, dataset, device="cpu")

    assert scores.family.shape == (3, 3)
    assert scores.node.shape == (3, 80)
    assert scores.edge.shape == (3, 0)
    assert scores.coverage.tolist() == [1, 1, 1]
    node = store.entities.node_index("beida-br-1")
    assert scores.node[:, node].tolist() == pytest.approx([0.70, 2.0, 4.0])


def test_score_timeline_ignores_padded_minutes(tmp_path) -> None:
    store = build_feature_store(
        [_cpu(0, 1.0), _cpu(1, 2.0)],
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    dataset = WindowDataset(store, window_minutes=5, stride_minutes=1)
    model = MultiSourceDetector(
        ModelDimensions(node_features=1, edge_features=0, log_features=0, hidden_size=8)
    )

    scores = score_timeline(model, dataset, device="cpu")

    assert scores.family.shape[0] == 2
    assert scores.coverage.tolist() == [1, 1]


import pytest

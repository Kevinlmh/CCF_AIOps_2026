from __future__ import annotations

from datetime import datetime, timedelta, timezone

import torch
from torch import nn
import numpy as np
import pytest

from aiops_challenge_2026.config import load_public_config
from aiops_v2.data.feature_store import build_feature_store
from aiops_v2.data.windows import WindowDataset
from aiops_v2.models.detector import ModelDimensions, MultiSourceDetector
from aiops_v2.training.inference import TimelineScores, score_timeline
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


class _WindowScorer(nn.Module):
    """Deterministic test scorer that gives overlapping windows distinct scores."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def forward(self, batch):
        self.calls += 1
        value = float(self.calls * 4 - 2)
        batch_size, minutes = batch["node_x"].shape[:2]
        nodes = batch["node_x"].shape[2]
        edges = batch["edge_x"].shape[2]

        def filled(shape):
            return torch.full(shape, value, dtype=torch.float32)

        output = {
            "family_anomaly": filled((batch_size, minutes, 3)),
            "node_anomaly": filled((batch_size, minutes, nodes)),
            "edge_anomaly": filled((batch_size, minutes, edges)),
            "log_anomaly": filled((batch_size, minutes, nodes)),
        }
        for name, entities in (("node", nodes), ("edge", edges), ("log", nodes)):
            for component in ("level", "reconstruction", "forecast"):
                output[f"{name}_{component}_anomaly"] = filled(
                    (batch_size, minutes, entities)
                )
        return output


def test_timeline_scores_keep_legacy_constructor_compatible() -> None:
    scores = TimelineScores(
        family=np.zeros((2, 3), dtype=np.float32),
        node=np.zeros((2, 1), dtype=np.float32),
        edge=np.zeros((2, 0), dtype=np.float32),
        log=np.zeros((2, 1), dtype=np.float32),
        coverage=np.ones(2, dtype=np.int32),
    )

    assert scores.family_observed.tolist() == [[True, True, True], [True, True, True]]
    assert scores.components == {}


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
    assert scores.coverage.tolist() == [1, 2, 1]
    node = store.entities.node_index("beida-br-1")
    assert scores.node[:, node].tolist() == pytest.approx([0.70, 1.70, 4.0])


def test_score_timeline_averages_overlap_and_preserves_masks_and_components(tmp_path) -> None:
    store = build_feature_store(
        [_cpu(0, 1.0), _cpu(1, 2.0), _cpu(2, 4.0)],
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    dataset = WindowDataset(store, window_minutes=2, stride_minutes=1)

    scores = score_timeline(_WindowScorer(), dataset, device="cpu")

    node = store.entities.node_index("beida-br-1")
    assert scores.coverage.tolist() == [1, 2, 1]
    assert scores.node[:, node].tolist() == pytest.approx([2.0, 4.0, 6.0])
    assert scores.components["node_level"][:, node].tolist() == pytest.approx(
        [2.0, 4.0, 6.0]
    )
    assert scores.family_observed.tolist() == [
        [True, False, False],
        [True, False, False],
        [True, False, False],
    ]


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

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
import torch

from aiops_challenge_2026.config import load_public_config
from aiops_v2.data.feature_store import build_feature_store
from aiops_v2.training.trainer import (
    TrainConfig,
    corrupt_observed_inputs,
    fit_timeline_calibrator,
    fit_store_scalers,
    load_checkpoint,
    save_checkpoint,
    train_detector,
)
from baseline.bian.preprocessing.observations import NumericObservation


START = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)


def test_masked_training_corrupts_only_model_input_and_preserves_targets() -> None:
    batch = {
        "node_x": torch.tensor([[[[1.0, 2.0]]]]),
        "node_mask": torch.tensor([[[[True, False]]]]),
        "edge_x": torch.empty((1, 1, 0, 0)),
        "edge_mask": torch.empty((1, 1, 0, 0), dtype=torch.bool),
        "log_x": torch.empty((1, 1, 1, 0)),
        "log_mask": torch.empty((1, 1, 1, 0), dtype=torch.bool),
        "time_x": torch.zeros((1, 1, 4)),
    }

    corrupted = corrupt_observed_inputs(batch, probability=1.0)

    assert batch["node_x"][0, 0, 0, 0] == 1.0
    assert bool(batch["node_mask"][0, 0, 0, 0]) is True
    assert corrupted["node_x"][0, 0, 0, 0] == 0.0
    assert bool(corrupted["node_mask"][0, 0, 0, 0]) is False
    assert corrupted["time_x"] is batch["time_x"]


def _observations() -> list[NumericObservation]:
    result = []
    for minute, value in enumerate((1.0, 1.2, 0.9, 1.1, 8.0, 1.0)):
        result.append(
            NumericObservation(
                timestamp=START + timedelta(minutes=minute),
                source="node",
                node_id="beida-br-1",
                related_node_ids=(),
                metric="node.cpu_usage",
                value=value,
                dimensions=(),
                direction="high",
            )
        )
    return result


def test_store_scaler_uses_boundary_reference_instead_of_fault_majority(tmp_path) -> None:
    values = [1.0] * 5 + [40.0] * 13 + [1.0] * 5
    observations = []
    for minute, value in enumerate(values):
        observations.append(
            NumericObservation(
                timestamp=START + timedelta(minutes=minute),
                source="node",
                node_id="beida-service-vm-1",
                related_node_ids=(),
                metric="node.cpu_usage",
                value=value,
                dimensions=(),
                direction="high",
            )
        )
    store = build_feature_store(
        observations,
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    scaler = fit_store_scalers(store)["node"]
    node = store.entities.node_index("beida-service-vm-1")
    feature = store.features.index("node", "node.cpu_usage")

    assert float(scaler.center[node, feature]) == 1.0


def test_timeline_calibrator_uses_boundary_reference_not_fault_plateau() -> None:
    scores = np.array([[1.0, 1.0, 0.0]] * 5 + [[40.0, 20.0, 0.0]] * 13 + [[1.0, 1.0, 0.0]] * 5)

    calibrator = fit_timeline_calibrator(scores)

    assert calibrator.center[0] == pytest.approx(np.log1p(1.0))


def test_training_produces_finite_history_scalers_and_calibrator(tmp_path) -> None:
    store = build_feature_store(
        _observations(),
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    artifact = train_detector(
        store,
        TrainConfig(epochs=1, batch_size=2, window_minutes=4, stride_minutes=2, hidden_size=8, seed=3),
    )

    assert len(artifact.history) == 1
    assert np.isfinite(artifact.history[0])
    assert set(artifact.scalers) == {"node", "edge", "log"}
    assert artifact.calibrator.center.shape == (3,)


def test_checkpoint_round_trip_preserves_model_and_preprocessing_state(tmp_path) -> None:
    store = build_feature_store(
        _observations(),
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    artifact = train_detector(
        store,
        TrainConfig(epochs=1, batch_size=2, window_minutes=4, stride_minutes=2, hidden_size=8, seed=4),
    )
    checkpoint = tmp_path / "model.pt"

    save_checkpoint(artifact, checkpoint, feature_manifest=store.manifest)
    restored = load_checkpoint(checkpoint, device="cpu")

    assert restored.config == artifact.config
    assert restored.scalers["node"].to_dict() == artifact.scalers["node"].to_dict()
    assert restored.calibrator.to_dict() == artifact.calibrator.to_dict()
    first_key = next(iter(artifact.model.state_dict()))
    assert np.allclose(
        artifact.model.state_dict()[first_key].detach().cpu().numpy(),
        restored.model.state_dict()[first_key].detach().cpu().numpy(),
    )

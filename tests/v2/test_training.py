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


def test_store_scaler_avoids_sparse_incident_bias_in_boundary_slices(tmp_path) -> None:
    values = [1.0] * 5 + [40.0] * 13
    observation_minutes = list(range(18))
    # Match the sparse public sample: only about 60 of 311 timeline minutes
    # contain this node's metric, and the incident happens in the first
    # boundary window. A boundary-only reference then over-represents it.
    later_minutes = np.linspace(61, 310, 42, dtype=int).tolist()
    values.extend([1.0] * len(later_minutes))
    observation_minutes.extend(later_minutes)
    observations = []
    for minute, value in zip(observation_minutes, values):
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

    default_scaler = fit_store_scalers(store)["node"]
    scaler = fit_store_scalers(store, transform_kind="asinh")["node"]
    node = store.entities.node_index("beida-service-vm-1")
    feature = store.features.index("node", "node.cpu_usage")

    assert float(scaler.center[node, feature]) == 1.0
    assert float(scaler.scale[node, feature]) == 1.0
    assert scaler.transform_kind == "asinh"
    assert default_scaler.transform_kind == "linear"


def test_training_reference_scaler_excludes_chronological_holdout(tmp_path) -> None:
    observations = [
        NumericObservation(
            timestamp=START + timedelta(minutes=minute),
            source="node",
            node_id="beida-br-1",
            related_node_ids=(),
            metric="node.cpu_usage",
            value=1.0 if minute < 45 else 100.0,
            dimensions=(),
            direction="high",
        )
        for minute in range(60)
    ]
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    node = store.entities.node_index("beida-br-1")
    feature = store.features.index("node", "node.cpu_usage")

    scaler = fit_store_scalers(store, reference_end=45)["node"]

    assert scaler.center[node, feature] == 1.0
    assert scaler.scale[node, feature] == 1.0


def test_training_temporal_holdout_records_validation_loss_and_purges_overlap(tmp_path) -> None:
    observations = [
        NumericObservation(
            timestamp=START + timedelta(minutes=minute),
            source="node",
            node_id="beida-br-1",
            related_node_ids=(),
            metric="node.cpu_usage",
            value=1.0 + (minute % 5) * 0.1,
            dimensions=(),
            direction="high",
        )
        for minute in range(72)
    ]
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )

    artifact = train_detector(
        store,
        TrainConfig(
            epochs=2,
            batch_size=2,
            window_minutes=8,
            stride_minutes=4,
            hidden_size=8,
            validation_fraction=0.25,
        ),
    )

    assert len(artifact.validation_history) == 2
    assert all(np.isfinite(value) for value in artifact.validation_history)
    assert artifact.temporal_split is not None
    assert artifact.temporal_split.boundary_index == 54
    assert artifact.temporal_split.training_window_count > 0
    assert artifact.temporal_split.validation_window_count > 0
    assert artifact.temporal_split.last_training_window_end <= 54
    assert artifact.temporal_split.first_validation_window_start >= 62

    checkpoint = tmp_path / "validated.pt"
    save_checkpoint(artifact, checkpoint, feature_manifest=store.manifest)
    restored = load_checkpoint(checkpoint)
    assert restored.validation_history == artifact.validation_history
    assert restored.temporal_split == artifact.temporal_split
    assert set(restored.entity_calibrators) == {"node", "edge", "log"}
    assert np.allclose(
        restored.entity_calibrators["node"].center,
        artifact.entity_calibrators["node"].center,
    )


def test_timeline_calibrator_uses_boundary_reference_not_fault_plateau() -> None:
    scores = np.array([[1.0, 1.0, 0.0]] * 5 + [[40.0, 20.0, 0.0]] * 13 + [[1.0, 1.0, 0.0]] * 5)

    calibrator = fit_timeline_calibrator(scores)

    assert calibrator.center[0] == pytest.approx(np.log1p(1.0))


def test_timeline_calibrator_uses_observed_mask_and_audits_reference_counts() -> None:
    scores = np.tile(np.array([1.0, 2.0, 1_000_000.0]), (20, 1))
    scores[-1, 0] = 40.0
    observed = np.ones_like(scores, dtype=bool)
    observed[:, 2] = False

    calibrator = fit_timeline_calibrator(scores, observed)
    changed = scores.copy()
    changed[:, 2] = 1e30
    repeated = fit_timeline_calibrator(changed, observed)

    assert calibrator.center.tolist() == pytest.approx(repeated.center.tolist())
    assert calibrator.scale.tolist() == pytest.approx(repeated.scale.tolist())
    assert calibrator.summary.valid_minute_count == (20, 20, 0)
    assert calibrator.summary.reference_minute_count == (12, 12, 0)


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


def test_detector_training_can_fit_synthetic_diagnosis_heads(tmp_path) -> None:
    store = build_feature_store(
        _observations(),
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    artifact = train_detector(
        store,
        TrainConfig(epochs=1, batch_size=2, window_minutes=4, stride_minutes=2, hidden_size=8, seed=13),
        taxonomy=load_public_config("fault_taxonomy"),
        diagnosis_examples_per_category=1,
        diagnosis_epochs=1,
        diagnosis_batch_size=32,
        diagnosis_hidden_size=8,
    )

    assert artifact.diagnosis_heads is not None
    assert artifact.diagnosis_summary is not None
    assert artifact.diagnosis_summary.training_example_count == 28
    assert len(artifact.diagnosis_history) == 1
    assert artifact.synthetic_template_version == "v1"


def test_checkpoint_round_trip_preserves_model_and_preprocessing_state(tmp_path) -> None:
    store = build_feature_store(
        _observations(),
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    artifact = train_detector(
        store,
        TrainConfig(epochs=1, batch_size=2, window_minutes=4, stride_minutes=2, hidden_size=8, seed=4, scaler_transform="asinh", score_pooling="topk"),
    )
    checkpoint = tmp_path / "model.pt"

    save_checkpoint(artifact, checkpoint, feature_manifest=store.manifest)
    restored = load_checkpoint(checkpoint, device="cpu")

    assert restored.config == artifact.config
    assert restored.scalers["node"].to_dict() == artifact.scalers["node"].to_dict()
    assert restored.scalers["node"].transform_kind == "asinh"
    assert restored.model.score_pooling == "topk"
    assert restored.calibrator.to_dict() == artifact.calibrator.to_dict()
    first_key = next(iter(artifact.model.state_dict()))
    assert np.allclose(
        artifact.model.state_dict()[first_key].detach().cpu().numpy(),
        restored.model.state_dict()[first_key].detach().cpu().numpy(),
    )

    # Checkpoints produced by the first asinh experiment stored the scaler
    # marker but predated the TrainConfig field. The loaded config must reflect
    # the actual preprocessing applied by that checkpoint.
    payload = torch.load(checkpoint, weights_only=True)
    del payload["train_config"]["scaler_transform"]
    torch.save(payload, checkpoint)
    migrated = load_checkpoint(checkpoint, device="cpu")
    assert migrated.config.scaler_transform == "asinh"
    assert migrated.model.score_pooling == "topk"


def test_checkpoint_v4_round_trip_preserves_diagnosis_head_and_taxonomy(tmp_path) -> None:
    taxonomy = load_public_config("fault_taxonomy")
    store = build_feature_store(
        _observations(),
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    artifact = train_detector(
        store,
        TrainConfig(epochs=1, batch_size=2, window_minutes=4, stride_minutes=2, hidden_size=8, seed=31),
        taxonomy=taxonomy,
        diagnosis_examples_per_category=1,
        diagnosis_epochs=1,
        diagnosis_batch_size=32,
        diagnosis_hidden_size=8,
    )
    checkpoint = tmp_path / "model_v4.pt"

    save_checkpoint(artifact, checkpoint, feature_manifest=store.manifest)
    restored = load_checkpoint(checkpoint)

    assert restored.diagnosis_heads is not None
    assert restored.taxonomy_identity == tuple(
        item["fault_name"] for item in taxonomy["fault_categories"]
    )
    assert restored.diagnosis_summary is not None
    head_key = next(iter(artifact.diagnosis_heads.state_dict()))
    assert torch.equal(
        artifact.diagnosis_heads.state_dict()[head_key],
        restored.diagnosis_heads.state_dict()[head_key],
    )


@pytest.mark.parametrize("legacy_version", [1, 2, 3])
def test_legacy_checkpoint_warns_and_never_claims_random_heads_are_trained(
    tmp_path,
    legacy_version: int,
) -> None:
    store = build_feature_store(
        _observations(),
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    artifact = train_detector(
        store,
        TrainConfig(epochs=1, batch_size=2, window_minutes=4, stride_minutes=2, hidden_size=8, seed=37),
    )
    checkpoint = tmp_path / "legacy.pt"
    save_checkpoint(artifact, checkpoint, feature_manifest=store.manifest)
    payload = torch.load(checkpoint, map_location="cpu", weights_only=True)
    payload["format_version"] = legacy_version
    torch.save(payload, checkpoint)

    with pytest.warns(UserWarning, match="no trained diagnosis heads"):
        restored = load_checkpoint(checkpoint)

    assert restored.diagnosis_heads is None

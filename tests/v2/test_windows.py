from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from aiops_challenge_2026.config import load_public_config
from aiops_v2.data.feature_store import build_feature_store
from aiops_v2.data.windows import RobustScaler, WindowDataset
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


def test_robust_scaler_ignores_missing_cells() -> None:
    values = np.array([[[1.0]], [[0.0]], [[3.0]], [[100.0]]], dtype=np.float32)
    mask = np.array([[[True]], [[False]], [[True]], [[True]]])

    scaler = RobustScaler.fit(values, mask)
    transformed = scaler.transform(values, mask)

    assert float(scaler.center[0, 0]) == 3.0
    assert float(transformed[1, 0, 0]) == 0.0
    assert bool(np.isfinite(transformed).all())


def test_robust_scaler_fits_each_entity_instead_of_mixing_node_baselines() -> None:
    values = np.array(
        [
            [[1.0], [1000.0]],
            [[2.0], [1100.0]],
            [[3.0], [1200.0]],
        ],
        dtype=np.float32,
    )
    mask = np.ones_like(values, dtype=bool)

    scaler = RobustScaler.fit(values, mask)
    transformed = scaler.transform(values, mask)

    assert scaler.center.tolist() == [[2.0], [1100.0]]
    assert transformed[1, 0, 0] == 0.0
    assert transformed[1, 1, 0] == 0.0


def test_asinh_scaler_compresses_extreme_observed_values_without_reordering() -> None:
    values = np.array([[[0.0]], [[1.0]], [[100000.0]], [[9999999.0]]], dtype=np.float32)
    mask = np.array([[[True]], [[True]], [[True]], [[False]]])
    scaler = RobustScaler(
        center=np.array([[0.0]], dtype=np.float32),
        scale=np.array([[1.0]], dtype=np.float32),
        transform_kind="asinh",
    )

    transformed = scaler.transform(values, mask)[:, 0, 0]

    assert transformed[0] == 0.0
    assert transformed[1] == pytest.approx(0.8813736)
    assert 10.0 < transformed[2] < 20.0
    assert transformed[0] < transformed[1] < transformed[2]
    assert transformed[3] == 0.0


def test_scaler_checkpoint_metadata_keeps_legacy_linear_behavior() -> None:
    legacy = RobustScaler.from_dict({"center": [[0.0]], "scale": [[1.0]]})
    value = np.array([[[100000.0]]], dtype=np.float32)
    mask = np.ones_like(value, dtype=bool)

    assert float(legacy.transform(value, mask)[0, 0, 0]) == 100000.0

    compressed = RobustScaler.from_dict(
        {"center": [[0.0]], "scale": [[1.0]], "transform_kind": "asinh"}
    )
    assert compressed.to_dict()["transform_kind"] == "asinh"
    assert float(compressed.transform(value, mask)[0, 0, 0]) < 20.0


def test_window_dataset_returns_fixed_tensor_shapes_and_time_features(tmp_path) -> None:
    store = build_feature_store(
        [_cpu(0, 1.0), _cpu(1, 2.0), _cpu(2, 3.0)],
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    dataset = WindowDataset(store, window_minutes=2, stride_minutes=1)

    first = dataset[0]

    assert len(dataset) == 2
    assert first["node_x"].shape == (2, 80, 1)
    assert first["node_mask"].shape == (2, 80, 1)
    assert first["edge_x"].shape == (2, 0, 0)
    assert first["log_x"].shape == (2, 80, 0)
    assert first["time_x"].shape == (2, 4)
    assert first["start_index"] == 0
    assert first["start_time"] == START


def test_window_dataset_pads_short_sequences_without_inventing_observations(tmp_path) -> None:
    store = build_feature_store(
        [_cpu(0, 1.0), _cpu(1, 2.0)],
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    dataset = WindowDataset(store, window_minutes=4, stride_minutes=1)

    window = dataset[0]

    assert len(dataset) == 1
    assert window["node_x"].shape == (4, 80, 1)
    assert not window["node_mask"][2:].any()
    assert np.all(window["node_x"][2:] == 0)

from __future__ import annotations

import torch

from aiops_v2.models.detector import (
    ModelDimensions,
    MultiSourceDetector,
    causal_forecast_error,
    self_supervised_loss,
)


def _batch(*, edge_features: int = 3, log_features: int = 2):
    torch.manual_seed(7)
    batch = {
        "node_x": torch.randn(2, 6, 4, 5),
        "node_mask": torch.ones(2, 6, 4, 5, dtype=torch.bool),
        "edge_x": torch.randn(2, 6, 3, edge_features),
        "edge_mask": torch.ones(2, 6, 3, edge_features, dtype=torch.bool),
        "log_x": torch.randn(2, 6, 4, log_features),
        "log_mask": torch.ones(2, 6, 4, log_features, dtype=torch.bool),
        "time_x": torch.randn(2, 6, 4),
    }
    return batch


def test_detector_returns_minute_entity_and_family_scores() -> None:
    batch = _batch()
    model = MultiSourceDetector(
        ModelDimensions(node_features=5, edge_features=3, log_features=2, hidden_size=16)
    )

    output = model(batch)

    assert output["fault_probability"].shape == (2, 6)
    assert output["node_anomaly"].shape == (2, 6, 4)
    assert output["edge_anomaly"].shape == (2, 6, 3)
    assert output["family_anomaly"].shape == (2, 6, 3)
    assert output["node_reconstruction"].shape == batch["node_x"].shape
    assert output["node_level_anomaly"].shape == (2, 6, 4)
    assert torch.all((0 <= output["fault_probability"]) & (output["fault_probability"] <= 1))


def test_level_anomaly_remains_high_through_a_sustained_shift() -> None:
    batch = _batch(edge_features=0, log_features=0)
    batch["node_x"].zero_()
    batch["node_x"][:, 1:, 0, 0] = 10.0
    model = MultiSourceDetector(
        ModelDimensions(node_features=5, edge_features=0, log_features=0, hidden_size=8)
    )

    output = model(batch)

    assert output["node_level_anomaly"][0, 0, 0] == 0.0
    assert torch.all(output["node_level_anomaly"][0, 1:, 0] == 2.0)


def test_single_extreme_feature_cannot_create_unbounded_entity_score() -> None:
    batch = _batch(edge_features=0, log_features=0)
    batch["node_x"].zero_()
    batch["node_x"][:, :, 0, 0] = 1_000_000.0
    model = MultiSourceDetector(
        ModelDimensions(node_features=5, edge_features=0, log_features=0, hidden_size=8)
    )

    output = model(batch)
    losses = self_supervised_loss(output, batch)

    assert float(output["node_level_anomaly"].max()) <= 5.0
    assert float(output["node_anomaly"].max().detach()) <= 25.0
    assert float(losses["total"].detach()) < 100.0


def test_causal_forecast_error_aligns_previous_prediction_to_current_minute() -> None:
    forecast = torch.tensor([[[[11.0]], [[40.0]], [[999.0]]]])
    target = torch.tensor([[[[1.0]], [[11.0]], [[40.0]]]])
    mask = torch.ones_like(target, dtype=torch.bool)

    error = causal_forecast_error(forecast, target, mask)

    assert error.tolist() == [[[0.0], [0.0], [0.0]]]


def test_self_supervised_loss_is_finite_and_backpropagates() -> None:
    batch = _batch()
    model = MultiSourceDetector(
        ModelDimensions(node_features=5, edge_features=3, log_features=2, hidden_size=12)
    )

    losses = self_supervised_loss(model(batch), batch)
    losses["total"].backward()

    assert torch.isfinite(losses["total"])
    assert losses["reconstruction"].item() >= 0
    assert losses["forecast"].item() >= 0
    assert any(parameter.grad is not None for parameter in model.parameters())


def test_detector_handles_absent_edge_and_log_feature_families() -> None:
    batch = _batch(edge_features=0, log_features=0)
    model = MultiSourceDetector(
        ModelDimensions(node_features=5, edge_features=0, log_features=0, hidden_size=8)
    )

    output = model(batch)
    losses = self_supervised_loss(output, batch)

    assert output["edge_reconstruction"].shape == (2, 6, 3, 0)
    assert output["log_reconstruction"].shape == (2, 6, 4, 0)
    assert torch.isfinite(losses["total"])


def test_missing_cells_do_not_change_loss_or_model_input() -> None:
    batch = _batch()
    batch["node_mask"][:, :, 0, 0] = False
    changed = {name: value.clone() for name, value in batch.items()}
    changed["node_x"][:, :, 0, 0] = 1_000_000.0
    model = MultiSourceDetector(
        ModelDimensions(node_features=5, edge_features=3, log_features=2, hidden_size=8)
    )
    model.eval()

    first = self_supervised_loss(model(batch), batch)["total"]
    second = self_supervised_loss(model(changed), changed)["total"]

    assert torch.allclose(first, second)

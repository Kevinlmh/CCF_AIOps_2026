"""Masked multi-source temporal autoencoder and anomaly scorer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import torch
from torch import Tensor, nn
import torch.nn.functional as functional


@dataclass(frozen=True, slots=True)
class ModelDimensions:
    node_features: int
    edge_features: int
    log_features: int
    time_features: int = 4
    hidden_size: int = 64
    temporal_layers: int = 3

    def __post_init__(self) -> None:
        if min(self.node_features, self.edge_features, self.log_features) < 0:
            raise ValueError("feature counts must be non-negative")
        if self.node_features == 0:
            raise ValueError("at least one node feature is required")
        if self.hidden_size < 4 or self.temporal_layers < 1:
            raise ValueError("hidden size and temporal layers are too small")


class _CausalBlock(nn.Module):
    def __init__(self, hidden_size: int, dilation: int) -> None:
        super().__init__()
        self.padding = 2 * dilation
        self.convolution = nn.Conv1d(
            hidden_size,
            hidden_size,
            kernel_size=3,
            dilation=dilation,
        )
        # Normalize channels independently at each minute. GroupNorm over
        # [channels, time] leaks future context into a causal forecast.
        self.normalization = nn.LayerNorm(hidden_size)

    def forward(self, values: Tensor) -> Tensor:
        residual = values
        values = functional.pad(values, (self.padding, 0))
        values = self.convolution(values)
        values = self.normalization(values.transpose(1, 2)).transpose(1, 2)
        return functional.gelu(values + residual)


class _TemporalBranch(nn.Module):
    def __init__(self, feature_count: int, hidden_size: int, layers: int) -> None:
        super().__init__()
        self.feature_count = feature_count
        if feature_count:
            self.projection = nn.Linear(feature_count * 2, hidden_size)
            self.temporal = nn.Sequential(
                *(_CausalBlock(hidden_size, 2**index) for index in range(layers))
            )
            self.reconstruction = nn.Linear(hidden_size, feature_count)
            self.forecast = nn.Linear(hidden_size, feature_count)

    def forward(self, values: Tensor, mask: Tensor, hidden_size: int) -> tuple[Tensor, Tensor, Tensor]:
        batch, time, entities, features = values.shape
        if features == 0 or entities == 0:
            hidden = values.new_zeros((batch, time, entities, hidden_size))
            empty = values.new_zeros(values.shape)
            return hidden, empty, empty
        masked = values * mask.to(values.dtype)
        inputs = torch.cat((masked, mask.to(values.dtype)), dim=-1)
        hidden = self.projection(inputs)
        hidden = hidden.permute(0, 2, 3, 1).reshape(batch * entities, hidden_size, time)
        hidden = self.temporal(hidden)
        hidden = hidden.reshape(batch, entities, hidden_size, time).permute(0, 3, 1, 2)
        return hidden, self.reconstruction(hidden), self.forecast(hidden)


def _masked_entity_error(
    prediction: Tensor, target: Tensor, mask: Tensor, *, pooling: str = "mean"
) -> Tensor:
    if target.shape[-1] == 0:
        return target.new_zeros(target.shape[:-1])
    weights = mask.to(target.dtype)
    errors = (prediction - target).abs().clamp_max(25.0).mul(weights)
    observed_count = weights.sum(dim=-1)
    if pooling == "topk":
        count = min(3, errors.shape[-1])
        selected = torch.topk(errors, count, dim=-1).values
        return 0.7 * selected[..., 0] + 0.3 * (
            selected.sum(dim=-1) / observed_count.clamp(min=1.0, max=float(count))
        )
    return errors.sum(dim=-1) / observed_count.clamp_min(1.0)


def causal_forecast_error(
    forecast: Tensor, target: Tensor, mask: Tensor, *, pooling: str = "mean"
) -> Tensor:
    """Align the prediction emitted at t-1 with the observation at t."""
    result = target.new_zeros(target.shape[:-1])
    if target.shape[1] < 2 or target.shape[-1] == 0:
        return result
    result[:, 1:] = _masked_entity_error(
        forecast[:, :-1],
        target[:, 1:],
        mask[:, 1:],
        pooling=pooling,
    )
    return result


def _topk_pool(values: Tensor, maximum: int = 3) -> Tensor:
    """Pool sparse incidents without averaging a lone fault across five entities."""
    if values.shape[-1] == 0:
        return values.new_zeros(values.shape[:-1])
    count = min(maximum, values.shape[-1])
    top = torch.topk(values, count, dim=-1).values
    return 0.7 * top[..., 0] + 0.3 * top.mean(dim=-1)


class MultiSourceDetector(nn.Module):
    """Node/edge/log normality model producing global and entity anomaly scores."""

    def __init__(self, dimensions: ModelDimensions, *, score_pooling: str = "mean") -> None:
        super().__init__()
        if score_pooling not in {"mean", "topk"}:
            raise ValueError("score_pooling must be mean or topk")
        self.dimensions = dimensions
        self.score_pooling = score_pooling
        hidden = dimensions.hidden_size
        layers = dimensions.temporal_layers
        self.node_branch = _TemporalBranch(dimensions.node_features, hidden, layers)
        self.edge_branch = _TemporalBranch(dimensions.edge_features, hidden, layers)
        self.log_branch = _TemporalBranch(dimensions.log_features, hidden, layers)
        self.graph_gate = nn.Linear(hidden * 2, hidden)
        self.log_fusion_gate = nn.Linear(hidden * 2, hidden)
        self.graph_enabled = True
        self.log_fusion_enabled = True
        self.time_context_enabled = True
        self.time_projection = nn.Linear(dimensions.time_features, hidden)

    def forward(self, batch: dict[str, Tensor]) -> dict[str, Tensor]:
        hidden_size = self.dimensions.hidden_size
        node_hidden, node_reconstruction, node_forecast = self.node_branch(
            batch["node_x"], batch["node_mask"], hidden_size
        )
        edge_hidden, edge_reconstruction, edge_forecast = self.edge_branch(
            batch["edge_x"], batch["edge_mask"], hidden_size
        )
        log_hidden, log_reconstruction, log_forecast = self.log_branch(
            batch["log_x"], batch["log_mask"], hidden_size
        )
        if self.log_fusion_enabled and self.log_branch.feature_count:
            log_observed = batch["log_mask"].any(dim=-1, keepdim=True).to(node_hidden.dtype)
            log_gate = torch.sigmoid(
                self.log_fusion_gate(torch.cat((node_hidden, log_hidden), dim=-1))
            )
            node_hidden = node_hidden + log_gate * log_hidden * log_observed
        graph_links = batch.get("graph_links")
        if (
            self.graph_enabled
            and graph_links is not None
            and graph_links.numel() > 0
            and edge_hidden.shape[2] > 0
        ):
            edge_indexes = graph_links[0].long()
            target_indexes = graph_links[1].long()
            if (
                int(edge_indexes.max()) >= edge_hidden.shape[2]
                or int(target_indexes.max()) >= node_hidden.shape[2]
                or int(edge_indexes.min()) < 0
                or int(target_indexes.min()) < 0
            ):
                raise ValueError("graph link index is outside the edge/node tensor")
            messages = edge_hidden.index_select(2, edge_indexes)
            aggregated = torch.zeros_like(node_hidden)
            counts = node_hidden.new_zeros((*node_hidden.shape[:-1], 1))
            aggregated.index_add_(2, target_indexes, messages)
            counts.index_add_(
                2,
                target_indexes,
                node_hidden.new_ones((*messages.shape[:-1], 1)),
            )
            aggregated = aggregated / counts.clamp_min(1.0)
            gate = torch.sigmoid(self.graph_gate(torch.cat((node_hidden, aggregated), dim=-1)))
            node_hidden = node_hidden + gate * aggregated
        time_context = self.time_projection(batch["time_x"]).unsqueeze(2)
        if self.time_context_enabled:
            node_hidden = node_hidden + time_context
        node_reconstruction = self.node_branch.reconstruction(node_hidden)
        node_forecast = self.node_branch.forecast(node_hidden)
        if self.edge_branch.feature_count:
            if self.time_context_enabled:
                edge_hidden = edge_hidden + time_context
            edge_reconstruction = self.edge_branch.reconstruction(edge_hidden)
            edge_forecast = self.edge_branch.forecast(edge_hidden)
        if self.log_branch.feature_count:
            if self.time_context_enabled:
                log_hidden = log_hidden + time_context
            log_reconstruction = self.log_branch.reconstruction(log_hidden)
            log_forecast = self.log_branch.forecast(log_hidden)
        node_reconstruction_anomaly = _masked_entity_error(
            node_reconstruction, batch["node_x"], batch["node_mask"], pooling=self.score_pooling
        )
        edge_reconstruction_anomaly = _masked_entity_error(
            edge_reconstruction, batch["edge_x"], batch["edge_mask"], pooling=self.score_pooling
        )
        log_reconstruction_anomaly = _masked_entity_error(
            log_reconstruction, batch["log_x"], batch["log_mask"], pooling=self.score_pooling
        )
        node_forecast_anomaly = causal_forecast_error(
            node_forecast, batch["node_x"], batch["node_mask"], pooling=self.score_pooling
        )
        edge_forecast_anomaly = causal_forecast_error(
            edge_forecast, batch["edge_x"], batch["edge_mask"], pooling=self.score_pooling
        )
        log_forecast_anomaly = causal_forecast_error(
            log_forecast, batch["log_x"], batch["log_mask"], pooling=self.score_pooling
        )
        node_level_anomaly = _masked_entity_error(
            torch.zeros_like(batch["node_x"]), batch["node_x"], batch["node_mask"], pooling=self.score_pooling
        )
        edge_level_anomaly = _masked_entity_error(
            torch.zeros_like(batch["edge_x"]), batch["edge_x"], batch["edge_mask"], pooling=self.score_pooling
        )
        log_level_anomaly = _masked_entity_error(
            torch.zeros_like(batch["log_x"]), batch["log_x"], batch["log_mask"], pooling=self.score_pooling
        )
        node_anomaly = (
            0.45 * node_level_anomaly
            + 0.25 * node_reconstruction_anomaly
            + 0.30 * node_forecast_anomaly
        )
        edge_anomaly = (
            0.45 * edge_level_anomaly
            + 0.25 * edge_reconstruction_anomaly
            + 0.30 * edge_forecast_anomaly
        )
        log_anomaly = (
            0.45 * log_level_anomaly
            + 0.25 * log_reconstruction_anomaly
            + 0.30 * log_forecast_anomaly
        )
        family_anomaly = torch.stack(
            (
                _topk_pool(node_anomaly),
                _topk_pool(edge_anomaly),
                _topk_pool(log_anomaly),
            ),
            dim=-1,
        )
        # Compatibility-only soft risk score. It is deliberately not consumed
        # by the pipeline; full-timeline calibrated probabilities are produced
        # by AnomalyCalibrator after overlapping windows have been assembled.
        ordered_families = torch.sort(family_anomaly, dim=-1).values
        diagnostic_logit = (
            0.75 * ordered_families[..., -1] + 0.25 * ordered_families[..., -2] - 4.0
        ) / 1.5
        return {
            "fault_probability": torch.sigmoid(diagnostic_logit),
            "node_anomaly": node_anomaly,
            "edge_anomaly": edge_anomaly,
            "log_anomaly": log_anomaly,
            "node_level_anomaly": node_level_anomaly,
            "edge_level_anomaly": edge_level_anomaly,
            "log_level_anomaly": log_level_anomaly,
            "node_reconstruction_anomaly": node_reconstruction_anomaly,
            "edge_reconstruction_anomaly": edge_reconstruction_anomaly,
            "log_reconstruction_anomaly": log_reconstruction_anomaly,
            "node_forecast_anomaly": node_forecast_anomaly,
            "edge_forecast_anomaly": edge_forecast_anomaly,
            "log_forecast_anomaly": log_forecast_anomaly,
            "family_anomaly": family_anomaly,
            "node_embedding": node_hidden,
            "edge_embedding": edge_hidden,
            "log_embedding": log_hidden,
            "node_reconstruction": node_reconstruction,
            "edge_reconstruction": edge_reconstruction,
            "log_reconstruction": log_reconstruction,
            "node_forecast": node_forecast,
            "edge_forecast": edge_forecast,
            "log_forecast": log_forecast,
        }


def _masked_mse(prediction: Tensor, target: Tensor, mask: Tensor) -> Tensor:
    weights = mask.to(target.dtype)
    if target.numel() == 0 or not bool(mask.any()):
        return prediction.sum() * 0.0
    robust = functional.smooth_l1_loss(prediction, target, reduction="none").clamp_max(25.0)
    return (robust * weights).sum() / weights.sum().clamp_min(1.0)


def _embedding_consistency(left: Tensor, right: Tensor, mask: Tensor) -> Tensor:
    if left.numel() == 0 or not bool(mask.any()):
        return left.sum() * 0.0
    left = functional.normalize(left, p=2, dim=-1)
    right = functional.normalize(right, p=2, dim=-1)
    distance = (left - right).pow(2).mean(dim=-1)
    weights = mask.to(distance.dtype)
    return (distance * weights).sum() / weights.sum().clamp_min(1.0)


def self_supervised_loss(
    output: dict[str, Tensor],
    batch: dict[str, Tensor],
    *,
    forecast_weight: float = 0.5,
    cross_source_weight: float = 0.05,
    topology_weight: float = 0.05,
) -> dict[str, Tensor]:
    """Reconstruct, forecast and align co-observed source/topology embeddings."""
    reconstruction = sum(
        (
            _masked_mse(output[f"{name}_reconstruction"], batch[f"{name}_x"], batch[f"{name}_mask"])
            for name in ("node", "edge", "log")
        )
    )
    forecast = sum(
        (
            _masked_mse(
                output[f"{name}_forecast"][:, :-1],
                batch[f"{name}_x"][:, 1:],
                batch[f"{name}_mask"][:, 1:],
            )
            for name in ("node", "edge", "log")
        )
    )
    cross_source = _embedding_consistency(
        output["node_embedding"],
        output["log_embedding"],
        batch["node_mask"].any(dim=-1) & batch["log_mask"].any(dim=-1),
    )
    topology = output["node_embedding"].sum() * 0.0
    links = batch.get("graph_links")
    if links is not None and links.numel() > 0:
        edge_indexes = links[0].long()
        target_indexes = links[1].long()
        edge_embedding = output["edge_embedding"].index_select(2, edge_indexes)
        target_embedding = output["node_embedding"].index_select(2, target_indexes)
        edge_observed = batch["edge_mask"].index_select(2, edge_indexes).any(dim=-1)
        target_observed = batch["node_mask"].index_select(2, target_indexes).any(dim=-1)
        topology = _embedding_consistency(
            target_embedding,
            edge_embedding,
            edge_observed & target_observed,
        )
    total = (
        reconstruction
        + forecast_weight * forecast
        + cross_source_weight * cross_source
        + topology_weight * topology
    )
    return {
        "total": total,
        "reconstruction": reconstruction,
        "forecast": forecast,
        "cross_source": cross_source,
        "topology": topology,
    }

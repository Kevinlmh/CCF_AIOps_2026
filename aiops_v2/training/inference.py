"""Sliding-window inference and overlap-safe timeline assembly."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import torch
from torch import Tensor, nn

from aiops_v2.data.windows import WindowDataset


@dataclass(frozen=True, slots=True)
class TimelineScores:
    family: np.ndarray
    node: np.ndarray
    edge: np.ndarray
    log: np.ndarray
    coverage: np.ndarray
    family_observed: np.ndarray = field(
        default_factory=lambda: np.empty((0, 3), dtype=bool)
    )
    components: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.family.ndim != 2 or self.family.shape[1] != 3:
            raise ValueError("family scores must have shape [T, 3]")
        if self.family_observed.size == 0:
            # Compatibility for callers constructing TimelineScores directly:
            # supplied scores are assumed observable when no mask is provided.
            object.__setattr__(
                self,
                "family_observed",
                np.ones(self.family.shape, dtype=bool),
            )
        elif self.family_observed.shape != self.family.shape:
            raise ValueError("family_observed shape must match family scores")


def _tensor(value: np.ndarray, device: torch.device) -> Tensor:
    result = torch.from_numpy(np.asarray(value))
    if result.dtype == torch.bool:
        return result.unsqueeze(0).to(device=device)
    return result.to(dtype=torch.float32).unsqueeze(0).to(device=device)


def score_timeline(
    model: nn.Module,
    dataset: WindowDataset,
    *,
    device: str | torch.device = "cpu",
) -> TimelineScores:
    """Average overlapping window scores on the original minute axis."""
    target = torch.device(device)
    model = model.to(target)
    model.eval()
    total = int(dataset.store.manifest["minute_count"])
    node_count = len(dataset.store.entities.nodes)
    edge_count = len(dataset.store.entities.edges)
    family_sum = np.zeros((total, 3), dtype=np.float64)
    node_sum = np.zeros((total, node_count), dtype=np.float64)
    edge_sum = np.zeros((total, edge_count), dtype=np.float64)
    log_sum = np.zeros((total, node_count), dtype=np.float64)
    component_names = {
        "node_level": ("node_level_anomaly", node_count),
        "node_reconstruction": ("node_reconstruction_anomaly", node_count),
        "node_forecast": ("node_forecast_anomaly", node_count),
        "edge_level": ("edge_level_anomaly", edge_count),
        "edge_reconstruction": ("edge_reconstruction_anomaly", edge_count),
        "edge_forecast": ("edge_forecast_anomaly", edge_count),
        "log_level": ("log_level_anomaly", node_count),
        "log_reconstruction": ("log_reconstruction_anomaly", node_count),
        "log_forecast": ("log_forecast_anomaly", node_count),
    }
    component_sums = {
        name: np.zeros((total, entity_count), dtype=np.float64)
        for name, (_, entity_count) in component_names.items()
    }
    coverage = np.zeros(total, dtype=np.int32)
    family_observed = np.zeros((total, 3), dtype=bool)

    with torch.no_grad():
        for item in range(len(dataset)):
            window = dataset[item]
            batch = {
                name: _tensor(window[name], target)
                for name in (
                    "node_x",
                    "node_mask",
                    "edge_x",
                    "edge_mask",
                    "log_x",
                    "log_mask",
                    "time_x",
                )
            }
            batch["graph_links"] = torch.from_numpy(window["graph_links"]).to(device=target)
            output = model(batch)
            start = int(window["start_index"])
            valid = min(dataset.window_minutes, total - start)
            stop = start + valid
            family_sum[start:stop] += output["family_anomaly"][0, :valid].cpu().numpy()
            node_sum[start:stop] += output["node_anomaly"][0, :valid].cpu().numpy()
            edge_sum[start:stop] += output["edge_anomaly"][0, :valid].cpu().numpy()
            log_sum[start:stop] += output["log_anomaly"][0, :valid].cpu().numpy()
            for name, (output_key, _) in component_names.items():
                component_sums[name][start:stop] += output[output_key][
                    0, :valid
                ].cpu().numpy()
            for family_index, family_name in enumerate(("node", "edge", "log")):
                observed = batch[f"{family_name}_mask"][0, :valid].any(dim=(1, 2))
                family_observed[start:stop, family_index] |= observed.cpu().numpy()
            coverage[start:stop] += 1

    if np.any(coverage == 0):
        raise RuntimeError("window configuration left uncovered timeline minutes")
    denominator = coverage.astype(np.float64)[:, None]
    return TimelineScores(
        family=(family_sum / denominator).astype(np.float32),
        node=(node_sum / denominator).astype(np.float32),
        edge=(edge_sum / denominator).astype(np.float32),
        log=(log_sum / denominator).astype(np.float32),
        coverage=coverage,
        family_observed=family_observed,
        components={
            name: (values / denominator).astype(np.float32)
            for name, values in component_sums.items()
        },
    )

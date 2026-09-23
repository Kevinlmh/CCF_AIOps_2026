"""Sliding-window inference and overlap-safe timeline assembly."""

from __future__ import annotations

from dataclasses import dataclass

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
    coverage = np.zeros(total, dtype=np.int32)

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
            local_start = 1 if start > 0 and coverage[start] > 0 else 0
            global_start = start + local_start
            stop = start + valid
            family_sum[global_start:stop] += output["family_anomaly"][
                0, local_start:valid
            ].cpu().numpy()
            node_sum[global_start:stop] += output["node_anomaly"][
                0, local_start:valid
            ].cpu().numpy()
            edge_sum[global_start:stop] += output["edge_anomaly"][
                0, local_start:valid
            ].cpu().numpy()
            log_sum[global_start:stop] += output["log_anomaly"][
                0, local_start:valid
            ].cpu().numpy()
            coverage[global_start:stop] += 1

    if np.any(coverage == 0):
        raise RuntimeError("window configuration left uncovered timeline minutes")
    denominator = coverage.astype(np.float64)[:, None]
    return TimelineScores(
        family=(family_sum / denominator).astype(np.float32),
        node=(node_sum / denominator).astype(np.float32),
        edge=(edge_sum / denominator).astype(np.float32),
        log=(log_sum / denominator).astype(np.float32),
        coverage=coverage,
    )

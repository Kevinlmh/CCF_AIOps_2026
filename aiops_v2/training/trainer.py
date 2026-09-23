"""Self-supervised training and portable checkpoint persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import random
from typing import Any

import numpy as np
import torch

from aiops_v2.data.feature_store import FeatureStore
from aiops_v2.data.windows import RobustScaler, WindowDataset
from aiops_v2.models.detector import (
    ModelDimensions,
    MultiSourceDetector,
    self_supervised_loss,
)
from aiops_v2.training.calibration import AnomalyCalibrator
from aiops_v2.training.inference import score_timeline


@dataclass(frozen=True, slots=True)
class TrainConfig:
    epochs: int = 5
    batch_size: int = 8
    learning_rate: float = 1e-3
    weight_decay: float = 1e-4
    window_minutes: int = 120
    stride_minutes: int = 30
    hidden_size: int = 64
    temporal_layers: int = 3
    mask_probability: float = 0.15
    seed: int = 2026
    device: str = "cpu"

    def __post_init__(self) -> None:
        if min(self.epochs, self.batch_size, self.window_minutes, self.stride_minutes) <= 0:
            raise ValueError("training counts and window sizes must be positive")
        if not 0 <= self.mask_probability <= 1:
            raise ValueError("mask probability must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class TrainingArtifact:
    model: MultiSourceDetector
    scalers: dict[str, RobustScaler]
    calibrator: AnomalyCalibrator
    config: TrainConfig
    history: tuple[float, ...]
    feature_manifest: dict[str, Any] | None = None


def fit_store_scalers(store: FeatureStore) -> dict[str, RobustScaler]:
    total = int(store.manifest["minute_count"])
    reference = min(60, max(3, total // 4))
    result = {}
    for name in ("node", "edge", "log"):
        values = np.asarray(getattr(store, f"{name}_values"))
        mask = np.asarray(getattr(store, f"{name}_mask"))
        if total > 2 * reference:
            reference_values = np.concatenate((values[:reference], values[-reference:]), axis=0)
            reference_mask = np.concatenate((mask[:reference], mask[-reference:]), axis=0)
        else:
            reference_values = values
            reference_mask = mask
        result[name] = RobustScaler.fit(reference_values, reference_mask)
    return result


def fit_timeline_calibrator(family_scores: np.ndarray) -> AnomalyCalibrator:
    """Fit on the lower-energy unlabeled majority, anchored by robust boundary scores.

    A boundary-only fit is fragile when a short public case begins or ends inside
    an incident. The initial boundary model ranks minutes by cross-family energy;
    the calibrator is then fit on the quietest 60% of the timeline. This relies on
    the task's sparse-event prior, not case labels or a target event count.
    """
    values = np.asarray(family_scores)
    total = values.shape[0]
    reference = min(60, max(3, total // 4))
    if total > 2 * reference:
        boundary = np.concatenate((values[:reference], values[-reference:]), axis=0)
    else:
        boundary = values
    initial = AnomalyCalibrator.fit(boundary)
    transformed = np.log1p(np.maximum(values.astype(np.float64), 0.0))
    standardized = np.maximum(
        (transformed - initial.center[None, :]) / initial.scale[None, :],
        0.0,
    )
    ordered = np.sort(standardized, axis=1)
    energy = 0.75 * ordered[:, -1] + 0.25 * ordered[:, -2]
    quiet_count = min(total, max(3, int(np.ceil(total * 0.60))))
    selected = np.argpartition(energy, quiet_count - 1)[:quiet_count]
    return AnomalyCalibrator.fit(values[selected])


def _stack_batch(dataset: WindowDataset, indices: list[int], device: torch.device):
    names = (
        "node_x",
        "node_mask",
        "edge_x",
        "edge_mask",
        "log_x",
        "log_mask",
        "time_x",
    )
    items = [dataset[index] for index in indices]
    result = {}
    for name in names:
        array = np.stack([item[name] for item in items])
        tensor = torch.from_numpy(array)
        if tensor.dtype != torch.bool:
            tensor = tensor.to(dtype=torch.float32)
        result[name] = tensor.to(device=device)
    result["graph_links"] = torch.from_numpy(dataset.graph_links).to(device=device)
    return result


def corrupt_observed_inputs(
    batch: dict[str, torch.Tensor],
    *,
    probability: float,
) -> dict[str, torch.Tensor]:
    """Hide a random subset from the encoder while leaving targets untouched."""
    if not 0 <= probability <= 1:
        raise ValueError("mask probability must be in [0, 1]")
    result = dict(batch)
    for name in ("node", "edge", "log"):
        values = batch[f"{name}_x"]
        mask = batch[f"{name}_mask"]
        if values.numel() == 0 or probability == 0:
            continue
        selected = mask & (torch.rand_like(values) < probability)
        corrupted_values = values.clone()
        corrupted_mask = mask.clone()
        corrupted_values[selected] = 0.0
        corrupted_mask[selected] = False
        result[f"{name}_x"] = corrupted_values
        result[f"{name}_mask"] = corrupted_mask
    return result


def train_detector(store: FeatureStore, config: TrainConfig | None = None) -> TrainingArtifact:
    settings = config or TrainConfig()
    random.seed(settings.seed)
    np.random.seed(settings.seed)
    torch.manual_seed(settings.seed)
    device = torch.device(settings.device)
    scalers = fit_store_scalers(store)
    dataset = WindowDataset(
        store,
        window_minutes=settings.window_minutes,
        stride_minutes=settings.stride_minutes,
        scalers=scalers,
    )
    dimensions = ModelDimensions(
        node_features=len(store.features.names("node")),
        edge_features=len(store.features.names("edge")),
        log_features=len(store.features.names("log")),
        hidden_size=settings.hidden_size,
        temporal_layers=settings.temporal_layers,
    )
    model = MultiSourceDetector(dimensions).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=settings.learning_rate,
        weight_decay=settings.weight_decay,
    )
    history = []
    indices = list(range(len(dataset)))
    for _ in range(settings.epochs):
        random.shuffle(indices)
        model.train()
        epoch_losses = []
        for offset in range(0, len(indices), settings.batch_size):
            batch = _stack_batch(dataset, indices[offset : offset + settings.batch_size], device)
            optimizer.zero_grad(set_to_none=True)
            model_input = corrupt_observed_inputs(batch, probability=settings.mask_probability)
            losses = self_supervised_loss(model(model_input), batch)
            losses["total"].backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            epoch_losses.append(float(losses["total"].detach().cpu()))
        history.append(float(np.mean(epoch_losses)))

    timeline = score_timeline(model, dataset, device=device)
    calibrator = fit_timeline_calibrator(timeline.family)
    return TrainingArtifact(
        model=model,
        scalers=scalers,
        calibrator=calibrator,
        config=settings,
        history=tuple(history),
        feature_manifest=store.manifest,
    )


def save_checkpoint(
    artifact: TrainingArtifact,
    path: Path,
    *,
    feature_manifest: dict[str, Any] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format_version": 3,
        "dimensions": asdict(artifact.model.dimensions),
        "model_state": artifact.model.state_dict(),
        "scalers": {name: scaler.to_dict() for name, scaler in artifact.scalers.items()},
        "calibrator": artifact.calibrator.to_dict(),
        "train_config": asdict(artifact.config),
        "history": list(artifact.history),
        "feature_manifest": feature_manifest or artifact.feature_manifest,
    }
    torch.save(payload, path)


def load_checkpoint(
    path: Path,
    *,
    device: str | torch.device = "cpu",
) -> TrainingArtifact:
    target = torch.device(device)
    payload = torch.load(path, map_location=target, weights_only=True)
    version = payload.get("format_version")
    if version not in {1, 2, 3}:
        raise ValueError("unsupported v2 checkpoint format")
    dimensions = ModelDimensions(**payload["dimensions"])
    model = MultiSourceDetector(dimensions).to(target)
    state = payload["model_state"]
    model.load_state_dict(state, strict=version >= 3)
    # Older checkpoints predate these fusion layers. Preserve their behavior
    # instead of silently enabling randomly initialized weights.
    model.graph_enabled = "graph_gate.weight" in state
    model.log_fusion_enabled = "log_fusion_gate.weight" in state
    model.time_context_enabled = "time_projection.weight" in state
    model.eval()
    return TrainingArtifact(
        model=model,
        scalers={name: RobustScaler.from_dict(value) for name, value in payload["scalers"].items()},
        calibrator=AnomalyCalibrator.from_dict(payload["calibrator"]),
        config=TrainConfig(**payload["train_config"]),
        history=tuple(float(value) for value in payload.get("history", [])),
        feature_manifest=payload.get("feature_manifest"),
    )

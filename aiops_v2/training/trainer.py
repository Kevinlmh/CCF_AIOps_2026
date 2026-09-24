"""Self-supervised training and portable checkpoint persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import random
from typing import Any
import warnings

import numpy as np
import torch

from aiops_v2.data.feature_store import FeatureStore
from aiops_v2.data.windows import RobustScaler, WindowDataset
from aiops_v2.diagnosis_schema import EVENT_FEATURE_NAMES, ROOT_FEATURE_NAMES
from aiops_v2.models.detector import (
    ModelDimensions,
    MultiSourceDetector,
    self_supervised_loss,
)
from aiops_v2.models.heads import EventDiagnosisHeads
from aiops_v2.training.calibration import AnomalyCalibrator, CalibrationSummary
from aiops_v2.training.inference import score_timeline
from aiops_v2.training.synthetic import generate_synthetic_diagnosis_data


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
    diagnosis_heads: EventDiagnosisHeads | None = None
    diagnosis_history: tuple[dict[str, float], ...] = ()
    diagnosis_summary: "DiagnosisTrainingSummary | None" = None
    synthetic_template_version: str | None = None
    taxonomy_identity: tuple[str, ...] | None = None

    @property
    def calibration_summary(self) -> CalibrationSummary | None:
        return self.calibrator.summary


@dataclass(frozen=True, slots=True)
class DiagnosisTrainingSummary:
    root_validation_accuracy: float
    category_validation_accuracy: float
    training_example_count: int
    validation_example_count: int
    epochs: int
    seed: int
    history: tuple[dict[str, float], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_validation_accuracy": self.root_validation_accuracy,
            "category_validation_accuracy": self.category_validation_accuracy,
            "training_example_count": self.training_example_count,
            "validation_example_count": self.validation_example_count,
            "epochs": self.epochs,
            "seed": self.seed,
            "history": [dict(item) for item in self.history],
        }


def fit_store_scalers(store: FeatureStore) -> dict[str, RobustScaler]:
    result = {}
    for name in ("node", "edge", "log"):
        values = np.asarray(getattr(store, f"{name}_values"))
        mask = np.asarray(getattr(store, f"{name}_mask"))
        # Boundary-only calibration is unsafe for sparse telemetry: a short
        # incident near the start/end can occupy most observations in those
        # slices and inflate the IQR until that metric no longer registers as
        # anomalous. Fit the per-entity/feature robust statistics on every
        # observed value instead; the median/IQR tolerate sparse (<25%) tails.
        result[name] = RobustScaler.fit(values, mask)
    return result


def fit_timeline_calibrator(
    family_scores: np.ndarray,
    observed_mask: np.ndarray | None = None,
) -> AnomalyCalibrator:
    """Fit on the lower-energy unlabeled majority, anchored by robust boundary scores.

    A boundary-only fit is fragile when a short public case begins or ends inside
    an incident. The initial boundary model ranks minutes by cross-family energy;
    the calibrator is then fit on the quietest 60% of the timeline. This relies on
    the task's sparse-event prior, not case labels or a target event count.
    """
    values = np.asarray(family_scores, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3 or values.shape[0] < 3:
        raise ValueError("timeline calibration requires [T, 3] with at least three minutes")
    observed = (
        np.ones(values.shape, dtype=bool)
        if observed_mask is None
        else np.asarray(observed_mask, dtype=bool)
    )
    if observed.shape != values.shape:
        raise ValueError("family observation mask must match timeline scores")
    if not np.isfinite(values[observed]).all():
        raise ValueError("observed timeline family scores must be finite")
    total = values.shape[0]
    reference = min(60, max(3, total // 4))
    if total > 2 * reference:
        boundary_values = np.concatenate((values[:reference], values[-reference:]), axis=0)
        boundary_mask = np.concatenate((observed[:reference], observed[-reference:]), axis=0)
    else:
        boundary_values = values
        boundary_mask = observed
    initial = AnomalyCalibrator.fit(boundary_values, boundary_mask)
    safe = np.where(observed, values, 0.0)
    transformed = np.log1p(np.maximum(safe, 0.0))
    standardized = np.maximum(
        (transformed - initial.center[None, :]) / initial.scale[None, :],
        0.0,
    )
    reference_mask = np.zeros(values.shape, dtype=bool)
    reference_counts = []
    valid_counts = observed.sum(axis=0).astype(np.int64)
    for family in range(values.shape[1]):
        valid_indexes = np.flatnonzero(observed[:, family])
        quiet_count = min(
            valid_indexes.size,
            max(1, int(np.ceil(valid_indexes.size * 0.60))),
        )
        if quiet_count:
            order = np.argsort(standardized[valid_indexes, family], kind="stable")
            selected = valid_indexes[order[:quiet_count]]
            reference_mask[selected, family] = True
        reference_counts.append(quiet_count)
    return AnomalyCalibrator.fit(
        values,
        reference_mask,
        reference_counts=tuple(reference_counts),
        valid_counts=tuple(int(item) for item in valid_counts),
    )


def fit_diagnosis_heads(
    taxonomy: dict[str, Any],
    root_feature_count: int,
    event_feature_count: int,
    *,
    seed: int,
    device: str,
    examples_per_category: int = 32,
    epochs: int = 12,
    batch_size: int = 64,
    hidden_size: int = 64,
) -> tuple[EventDiagnosisHeads, DiagnosisTrainingSummary]:
    """Train small candidate-conditioned heads only on semantic injections."""
    if min(examples_per_category, epochs, batch_size, hidden_size) <= 0:
        raise ValueError("diagnosis training sizes must be positive")
    if root_feature_count != len(ROOT_FEATURE_NAMES):
        raise ValueError("root feature count does not match diagnosis schema")
    if event_feature_count != len(EVENT_FEATURE_NAMES):
        raise ValueError("event feature count does not match diagnosis schema")
    categories = taxonomy.get("fault_categories", [])
    if not categories:
        raise ValueError("taxonomy must contain fault categories")

    training_examples = generate_synthetic_diagnosis_data(
        taxonomy,
        examples_per_category=examples_per_category,
        seed=seed,
        root_feature_names=ROOT_FEATURE_NAMES,
        event_feature_names=EVENT_FEATURE_NAMES,
    )
    validation_per_category = max(1, examples_per_category // 8)
    validation_examples = generate_synthetic_diagnosis_data(
        taxonomy,
        examples_per_category=validation_per_category,
        seed=seed + 1,
        root_feature_names=ROOT_FEATURE_NAMES,
        event_feature_names=EVENT_FEATURE_NAMES,
    )
    target = torch.device(device)
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    def tensors(examples):
        candidate_features = torch.from_numpy(
            np.stack([item.candidate_features for item in examples])
        ).to(device=target)
        event_features = torch.from_numpy(
            np.stack([item.event_features for item in examples])
        ).to(device=target)
        candidate_mask = torch.from_numpy(
            np.stack([item.candidate_mask for item in examples])
        ).to(device=target)
        root_labels = torch.tensor(
            [item.root_index for item in examples], dtype=torch.long, device=target
        )
        category_labels = torch.tensor(
            [item.category_index for item in examples], dtype=torch.long, device=target
        )
        return candidate_features, event_features, candidate_mask, root_labels, category_labels

    train = tensors(training_examples)
    valid = tensors(validation_examples)
    model = EventDiagnosisHeads(
        root_feature_count,
        event_feature_count,
        len(categories),
        hidden_size=hidden_size,
    ).to(target)
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)
    rng = np.random.default_rng(seed)
    history: list[dict[str, float]] = []
    for _ in range(epochs):
        model.train()
        order = rng.permutation(len(training_examples))
        batch_losses = []
        for offset in range(0, len(order), batch_size):
            selected = torch.as_tensor(order[offset : offset + batch_size], device=target)
            candidates, context, mask, roots, labels = (
                item.index_select(0, selected) for item in train
            )
            root_logits, category_logits = model(candidates, context, mask)
            row = torch.arange(roots.shape[0], device=target)
            category_for_root = category_logits[row, roots]
            loss = torch.nn.functional.cross_entropy(root_logits, roots)
            loss = loss + torch.nn.functional.cross_entropy(category_for_root, labels)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            batch_losses.append(float(loss.detach().cpu()))

        model.eval()
        with torch.no_grad():
            candidates, context, mask, roots, labels = valid
            root_logits, category_logits = model(candidates, context, mask)
            rows = torch.arange(roots.shape[0], device=target)
            category_for_root = category_logits[rows, roots]
            history.append(
                {
                    "loss": float(np.mean(batch_losses)),
                    "root_validation_accuracy": float(
                        (root_logits.argmax(dim=1) == roots).float().mean().cpu()
                    ),
                    "category_validation_accuracy": float(
                        (category_for_root.argmax(dim=1) == labels).float().mean().cpu()
                    ),
                }
            )

    model.eval()
    final = history[-1]
    summary = DiagnosisTrainingSummary(
        root_validation_accuracy=final["root_validation_accuracy"],
        category_validation_accuracy=final["category_validation_accuracy"],
        training_example_count=len(training_examples),
        validation_example_count=len(validation_examples),
        epochs=epochs,
        seed=seed,
        history=tuple(history),
    )
    return model, summary


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


def train_detector(
    store: FeatureStore,
    config: TrainConfig | None = None,
    *,
    taxonomy: dict[str, Any] | None = None,
    diagnosis_examples_per_category: int = 32,
    diagnosis_epochs: int = 12,
    diagnosis_batch_size: int = 64,
    diagnosis_hidden_size: int = 64,
) -> TrainingArtifact:
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
    calibrator = fit_timeline_calibrator(timeline.family, timeline.family_observed)
    diagnosis_heads = None
    diagnosis_summary = None
    if taxonomy is not None:
        diagnosis_heads, diagnosis_summary = fit_diagnosis_heads(
            taxonomy,
            len(ROOT_FEATURE_NAMES),
            len(EVENT_FEATURE_NAMES),
            seed=settings.seed,
            device=settings.device,
            examples_per_category=diagnosis_examples_per_category,
            epochs=diagnosis_epochs,
            batch_size=diagnosis_batch_size,
            hidden_size=diagnosis_hidden_size,
        )
    return TrainingArtifact(
        model=model,
        scalers=scalers,
        calibrator=calibrator,
        config=settings,
        history=tuple(history),
        feature_manifest=store.manifest,
        diagnosis_heads=diagnosis_heads,
        diagnosis_history=diagnosis_summary.history if diagnosis_summary else (),
        diagnosis_summary=diagnosis_summary,
        synthetic_template_version="v1" if diagnosis_summary else None,
        taxonomy_identity=(
            tuple(item["fault_name"] for item in taxonomy["fault_categories"])
            if taxonomy is not None
            else None
        ),
    )


def save_checkpoint(
    artifact: TrainingArtifact,
    path: Path,
    *,
    feature_manifest: dict[str, Any] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format_version": 4,
        "dimensions": asdict(artifact.model.dimensions),
        "model_state": artifact.model.state_dict(),
        "scalers": {name: scaler.to_dict() for name, scaler in artifact.scalers.items()},
        "calibrator": artifact.calibrator.to_dict(),
        "train_config": asdict(artifact.config),
        "history": list(artifact.history),
        "feature_manifest": feature_manifest or artifact.feature_manifest,
        "diagnosis_head_config": (
            {
                "root_feature_count": artifact.diagnosis_heads.root_feature_count,
                "event_feature_count": artifact.diagnosis_heads.event_feature_count,
                "category_count": artifact.diagnosis_heads.category_count,
                "hidden_size": artifact.diagnosis_heads.hidden_size,
            }
            if artifact.diagnosis_heads is not None
            else None
        ),
        "diagnosis_head_state": (
            artifact.diagnosis_heads.state_dict()
            if artifact.diagnosis_heads is not None
            else None
        ),
        "root_feature_names": list(ROOT_FEATURE_NAMES),
        "event_feature_names": list(EVENT_FEATURE_NAMES),
        "taxonomy_identity": list(artifact.taxonomy_identity or ()),
        "diagnosis_history": [dict(item) for item in artifact.diagnosis_history],
        "diagnosis_summary": (
            artifact.diagnosis_summary.to_dict()
            if artifact.diagnosis_summary is not None
            else None
        ),
        "synthetic_template_version": artifact.synthetic_template_version,
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
    if version not in {1, 2, 3, 4}:
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
    diagnosis_heads = None
    diagnosis_history: tuple[dict[str, float], ...] = ()
    diagnosis_summary = None
    taxonomy_identity = None
    synthetic_template_version = None
    if version == 4:
        if payload.get("root_feature_names") != list(ROOT_FEATURE_NAMES):
            raise ValueError("checkpoint root feature schema does not match v2.0")
        if payload.get("event_feature_names") != list(EVENT_FEATURE_NAMES):
            raise ValueError("checkpoint event feature schema does not match v2.0")
        head_config = payload.get("diagnosis_head_config")
        head_state = payload.get("diagnosis_head_state")
        if (head_config is None) != (head_state is None):
            raise ValueError("checkpoint diagnosis head config/state are incomplete")
        if head_config is not None:
            diagnosis_heads = EventDiagnosisHeads(**head_config).to(target)
            diagnosis_heads.load_state_dict(head_state, strict=True)
            diagnosis_heads.eval()
        taxonomy_identity = tuple(payload.get("taxonomy_identity", ())) or None
        diagnosis_history = tuple(
            {str(name): float(value) for name, value in item.items()}
            for item in payload.get("diagnosis_history", [])
        )
        summary_payload = payload.get("diagnosis_summary")
        if summary_payload is not None:
            diagnosis_summary = DiagnosisTrainingSummary(
                root_validation_accuracy=float(summary_payload["root_validation_accuracy"]),
                category_validation_accuracy=float(summary_payload["category_validation_accuracy"]),
                training_example_count=int(summary_payload["training_example_count"]),
                validation_example_count=int(summary_payload["validation_example_count"]),
                epochs=int(summary_payload["epochs"]),
                seed=int(summary_payload["seed"]),
                history=tuple(
                    {str(name): float(value) for name, value in item.items()}
                    for item in summary_payload.get("history", [])
                ),
            )
        synthetic_template_version = payload.get("synthetic_template_version")
    else:
        warnings.warn(
            f"v2 checkpoint format {version} has no trained diagnosis heads; "
            "prediction will use the deterministic candidate-evidence fallback",
            UserWarning,
            stacklevel=2,
        )
    return TrainingArtifact(
        model=model,
        scalers={name: RobustScaler.from_dict(value) for name, value in payload["scalers"].items()},
        calibrator=AnomalyCalibrator.from_dict(payload["calibrator"]),
        config=TrainConfig(**payload["train_config"]),
        history=tuple(float(value) for value in payload.get("history", [])),
        feature_manifest=payload.get("feature_manifest"),
        diagnosis_heads=diagnosis_heads,
        diagnosis_history=diagnosis_history,
        diagnosis_summary=diagnosis_summary,
        synthetic_template_version=synthetic_template_version,
        taxonomy_identity=taxonomy_identity,
    )

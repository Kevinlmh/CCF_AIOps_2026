"""Global semi-Markov decoder for non-overlapping fault intervals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math

import numpy as np


@dataclass(frozen=True, slots=True)
class DecoderConfig:
    open_threshold: float = 0.80
    keep_threshold: float = 0.40
    event_penalty: float = 0.55
    peak_bonus: float = 0.50
    boundary_bonus: float = 0.50
    change_threshold: float = 0.20
    max_duration_minutes: int = 30
    preferred_gap_minutes: int = 20
    close_event_penalty: float = 0.50
    duration_weight: float = 0.08
    recovery_weight: float = 0.30
    evidence_weight: float = 0.20
    evidence_z_threshold: float = 3.0

    def __post_init__(self) -> None:
        if not 0 <= self.keep_threshold < self.open_threshold <= 1:
            raise ValueError("decoder thresholds must satisfy 0 <= keep < open <= 1")
        if self.max_duration_minutes < 1 or self.preferred_gap_minutes < 0:
            raise ValueError("decoder durations must be non-negative")
        if min(self.duration_weight, self.recovery_weight, self.evidence_weight) < 0:
            raise ValueError("decoder score weights must be non-negative")
        if self.evidence_z_threshold < 0:
            raise ValueError("evidence z threshold must be non-negative")


@dataclass(frozen=True, slots=True)
class DecoderEvidence:
    family_z_scores: np.ndarray
    family_observed: np.ndarray
    node: np.ndarray | None = None
    edge: np.ndarray | None = None
    log: np.ndarray | None = None

    def __post_init__(self) -> None:
        scores = np.asarray(self.family_z_scores)
        observed = np.asarray(self.family_observed, dtype=bool)
        if scores.ndim != 2 or scores.shape[1] != 3 or observed.shape != scores.shape:
            raise ValueError("decoder family evidence must have matching [T, 3] arrays")
        if not np.isfinite(scores[observed]).all():
            raise ValueError("observed decoder family evidence must be finite")
        for name in ("node", "edge", "log"):
            value = getattr(self, name)
            if value is None:
                continue
            array = np.asarray(value)
            if array.ndim != 2 or array.shape[0] != scores.shape[0]:
                raise ValueError(f"decoder {name} evidence must have shape [T, entity]")
            if not np.isfinite(array).all():
                raise ValueError(f"decoder {name} evidence must be finite")


@dataclass(frozen=True, slots=True)
class DecodedEvent:
    start_index: int
    end_index: int
    peak_index: int
    confidence: float
    score: float
    start_time: datetime
    end_time: datetime
    evidence_scores: dict[str, float] = field(default_factory=dict)
    city_id: str | None = None
    root_city_scoped: bool = False

    @property
    def duration_minutes(self) -> int:
        return self.end_index - self.start_index + 1


def reconcile_events(events: tuple[DecodedEvent, ...]) -> tuple[DecodedEvent, ...]:
    """Keep the strongest compatible city candidates on a shared timeline.

    Decoder scores from overlapping cities describe competing explanations
    for one incident, so adding their scores would count the same evidence
    more than once. A direct node trigger breaks otherwise equal scores.
    """
    selected: list[DecodedEvent] = []
    for event in sorted(
        events,
        key=lambda item: (
            -item.score,
            -int(item.root_city_scoped),
            -item.confidence,
            item.start_index,
            item.end_index,
            item.city_id or "",
        ),
    ):
        if all(
            event.end_index < kept.start_index or kept.end_index < event.start_index
            for kept in selected
        ):
            selected.append(event)
    selected.sort(key=lambda item: (item.start_index, item.city_id or "", item.end_index))
    return tuple(selected)


@dataclass(frozen=True, slots=True)
class _Candidate:
    start: int
    end: int
    peak: int
    confidence: float
    score: float
    evidence_scores: dict[str, float]


def _candidate_starts(values: np.ndarray, config: DecoderConfig) -> tuple[int, ...]:
    starts = []
    for index, probability in enumerate(values):
        previous = float(values[index - 1]) if index else 0.0
        if probability < config.keep_threshold:
            continue
        opens_high_confidence = probability >= config.open_threshold and (
            index == 0
            or previous < config.open_threshold
            or probability - previous >= config.change_threshold
        )
        opens_persistent_candidate = (
            index > 0
            and previous < config.keep_threshold
            and probability - previous >= config.change_threshold
        )
        if opens_high_confidence or opens_persistent_candidate:
            starts.append(index)
    return tuple(starts)


def _candidate_evidence_scores(
    start: int,
    end: int,
    following: float,
    evidence: DecoderEvidence | None,
    config: DecoderConfig,
) -> dict[str, float]:
    result = {
        "family_support": 0.0,
        "node_peak": 0.0,
        "edge_peak": 0.0,
        "log_peak": 0.0,
    }
    if evidence is None:
        return result
    family_z = np.asarray(evidence.family_z_scores[start : end + 1], dtype=np.float64)
    observed = np.asarray(evidence.family_observed[start : end + 1], dtype=bool)
    available_count = observed.sum(axis=1)
    multi_source = available_count >= 2
    if np.any(multi_source):
        supported_count = ((family_z >= config.evidence_z_threshold) & observed).sum(axis=1)
        fractions = supported_count[multi_source] / available_count[multi_source]
        result["family_support"] = float(np.mean(fractions))
    for name in ("node", "edge", "log"):
        values = getattr(evidence, name)
        if values is not None:
            selected = np.asarray(values[start : end + 1], dtype=np.float64)
            result[f"{name}_peak"] = float(np.max(selected)) if selected.size else 0.0
    return result


def _candidates(
    values: np.ndarray,
    config: DecoderConfig,
    evidence: DecoderEvidence | None = None,
) -> dict[int, tuple[_Candidate, ...]]:
    result: dict[int, tuple[_Candidate, ...]] = {}
    size = values.shape[0]
    for start in _candidate_starts(values, config):
        items = []
        stop = min(size, start + config.max_duration_minutes)
        for end in range(start, stop):
            window = values[start : end + 1]
            peak_relative = int(np.argmax(window))
            peak = start + peak_relative
            has_previous = start > 0
            has_following = end + 1 < size
            previous = float(values[start - 1]) if has_previous else float(values[start])
            following = float(values[end + 1]) if has_following else float(values[end])
            excess = np.maximum(window - config.keep_threshold, 0.0)
            duration_minutes = end - start + 1
            interval_term = float(np.mean(excess) * math.sqrt(duration_minutes))
            duration_term = config.duration_weight * min(
                math.log1p(duration_minutes - 1),
                math.log1p(config.max_duration_minutes - 1),
            )
            peak_term = config.peak_bonus * max(0.0, float(values[peak]) - config.open_threshold)
            onset_score = max(0.0, float(values[start]) - previous)
            boundary_term = (
                config.boundary_bonus * onset_score
                if has_previous and float(values[start]) >= config.open_threshold
                else 0.0
            )
            boundary_term += (
                config.boundary_bonus * max(0.0, float(values[end]) - following)
                if has_following and float(values[end]) >= config.open_threshold
                else 0.0
            )
            recovery_drop = (
                max(0.0, float(values[end]) - following) if has_following else 0.0
            )
            interval_evidence = _candidate_evidence_scores(
                start, end, following, evidence, config
            )
            support_term = config.evidence_weight * interval_evidence["family_support"]
            recovery_term = config.recovery_weight * recovery_drop
            score = (
                interval_term
                + duration_term
                + peak_term
                + boundary_term
                + recovery_term
                + support_term
                - config.event_penalty
            )
            if score <= 0:
                continue
            components = {
                "onset": onset_score,
                "interval": interval_term,
                "recovery_drop": recovery_drop,
                "recovery_term": recovery_term,
                "duration_term": duration_term,
                "peak_term": peak_term,
                "boundary_term": boundary_term,
                "event_penalty": config.event_penalty,
                **interval_evidence,
                "selected_score": score,
            }
            items.append(
                _Candidate(
                    start=start,
                    end=end,
                    peak=peak,
                    confidence=float(np.mean(window)),
                    score=score,
                    evidence_scores=components,
                )
            )
        if items:
            result[start] = tuple(items)
    return result


def decode_events(
    probabilities: np.ndarray,
    origin: datetime,
    config: DecoderConfig | None = None,
    *,
    evidence: DecoderEvidence | None = None,
) -> tuple[DecodedEvent, ...]:
    """Select a globally optimal, non-overlapping event sequence."""
    settings = config or DecoderConfig()
    values = np.asarray(probabilities, dtype=np.float64)
    if values.ndim != 1:
        raise ValueError("decoder expects a one-dimensional probability sequence")
    if not np.isfinite(values).all():
        raise ValueError("decoder requires finite probabilities")
    if np.any(values < 0) or np.any(values > 1):
        raise ValueError("decoder probabilities must lie in [0, 1]")
    if evidence is not None and np.asarray(evidence.family_z_scores).shape[0] != values.size:
        raise ValueError("decoder evidence timeline length must match probabilities")
    if values.size == 0:
        return ()

    candidates = _candidates(values, settings, evidence)
    gap_cap = settings.preferred_gap_minutes
    negative = -math.inf
    scores = np.full((values.size + 1, gap_cap + 1), negative, dtype=np.float64)
    parents: list[list[tuple[int, int, _Candidate | None] | None]] = [
        [None] * (gap_cap + 1) for _ in range(values.size + 1)
    ]
    scores[0, gap_cap] = 0.0

    for position in range(values.size):
        for gap in range(gap_cap + 1):
            current = float(scores[position, gap])
            if not math.isfinite(current):
                continue
            next_gap = min(gap_cap, gap + 1)
            if current > scores[position + 1, next_gap]:
                scores[position + 1, next_gap] = current
                parents[position + 1][next_gap] = (position, gap, None)
            for candidate in candidates.get(position, ()):
                close_fraction = 0.0 if gap_cap == 0 else (gap_cap - gap) / gap_cap
                transition = current + candidate.score - settings.close_event_penalty * close_fraction
                next_position = candidate.end + 1
                if transition > scores[next_position, 0]:
                    scores[next_position, 0] = transition
                    parents[next_position][0] = (position, gap, candidate)

    gap = int(np.argmax(scores[values.size]))
    position = values.size
    selected: list[_Candidate] = []
    while position > 0:
        parent = parents[position][gap]
        if parent is None:
            break
        previous_position, previous_gap, candidate = parent
        if candidate is not None:
            selected.append(candidate)
        position, gap = previous_position, previous_gap
    selected.reverse()
    return tuple(
        DecodedEvent(
            start_index=item.start,
            end_index=item.end,
            peak_index=item.peak,
            confidence=min(1.0, max(0.0, item.confidence)),
            score=item.score,
            start_time=origin + timedelta(minutes=item.start),
            end_time=origin + timedelta(minutes=item.end + 1),
            evidence_scores=item.evidence_scores,
        )
        for item in selected
    )

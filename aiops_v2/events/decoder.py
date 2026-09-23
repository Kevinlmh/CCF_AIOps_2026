"""Global semi-Markov decoder for non-overlapping fault intervals."""

from __future__ import annotations

from dataclasses import dataclass
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

    def __post_init__(self) -> None:
        if not 0 <= self.keep_threshold < self.open_threshold <= 1:
            raise ValueError("decoder thresholds must satisfy 0 <= keep < open <= 1")
        if self.max_duration_minutes < 1 or self.preferred_gap_minutes < 0:
            raise ValueError("decoder durations must be non-negative")


@dataclass(frozen=True, slots=True)
class DecodedEvent:
    start_index: int
    end_index: int
    peak_index: int
    confidence: float
    score: float
    start_time: datetime
    end_time: datetime

    @property
    def duration_minutes(self) -> int:
        return self.end_index - self.start_index + 1


@dataclass(frozen=True, slots=True)
class _Candidate:
    start: int
    end: int
    peak: int
    confidence: float
    score: float


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


def _candidates(values: np.ndarray, config: DecoderConfig) -> dict[int, tuple[_Candidate, ...]]:
    result: dict[int, tuple[_Candidate, ...]] = {}
    size = values.shape[0]
    for start in _candidate_starts(values, config):
        items = []
        stop = min(size, start + config.max_duration_minutes)
        for end in range(start, stop):
            window = values[start : end + 1]
            peak_relative = int(np.argmax(window))
            peak = start + peak_relative
            previous = float(values[start - 1]) if start else 0.0
            following = float(values[end + 1]) if end + 1 < size else 0.0
            core = float(np.sum(window - config.keep_threshold))
            peak_term = config.peak_bonus * max(0.0, float(values[peak]) - config.open_threshold)
            boundary_term = config.boundary_bonus * (
                max(0.0, float(values[start]) - previous)
                if float(values[start]) >= config.open_threshold
                else 0.0
            )
            boundary_term += config.boundary_bonus * (
                max(0.0, float(values[end]) - following)
                if float(values[end]) >= config.open_threshold
                else 0.0
            )
            score = core + peak_term + boundary_term - config.event_penalty
            if score <= 0:
                continue
            items.append(
                _Candidate(
                    start=start,
                    end=end,
                    peak=peak,
                    confidence=float(np.mean(window)),
                    score=score,
                )
            )
        if items:
            result[start] = tuple(items)
    return result


def decode_events(
    probabilities: np.ndarray,
    origin: datetime,
    config: DecoderConfig | None = None,
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
    if values.size == 0:
        return ()

    candidates = _candidates(values, settings)
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
        )
        for item in selected
    )

"""Robust multi-source anomaly scoring and bounded event segmentation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatch
import math
from statistics import median
from typing import Any, Iterable

from ..preprocessing.multisource import ObservationBundle
from ..preprocessing.observations import AnomalyEvidence, DetectedEvent, NumericObservation


@dataclass(frozen=True, slots=True)
class DetectionDiagnostics:
    minute_energy: dict[datetime, float]
    source_energy: dict[datetime, dict[str, float]]
    evidence_count: int
    source_coverage: dict[str, int]
    observation_start: datetime | None
    observation_end: datetime | None
    open_threshold: float
    keep_threshold: float


def _finite_history(history: Iterable[float | None]) -> list[float]:
    return [
        float(value)
        for value in history
        if value is not None and math.isfinite(float(value))
    ]


def robust_score(
    value: float,
    history: Iterable[float | None],
    config: dict[str, Any],
    *,
    direction: str = "both",
) -> tuple[float, float]:
    """Return rolling-median baseline and a finite directional robust score."""
    clean = _finite_history(history)
    if not clean:
        return value, 0.0
    center = float(median(clean))
    deviations = [abs(item - center) for item in clean]
    mad_scale = 1.4826 * float(median(deviations))
    absolute_floor = float(config.get("absolute_scale_floor", 0.01))
    relative_floor = float(config.get("relative_scale_floor", 0.01))
    scale = max(mad_scale, absolute_floor, relative_floor * max(1.0, abs(center)))
    if direction == "high":
        difference = max(0.0, value - center)
    elif direction == "low":
        difference = max(0.0, center - value)
    else:
        difference = abs(value - center)
    maximum = float(config.get("max_score", 25.0))
    return center, min(maximum, difference / scale)


def _consolidate_series(
    observations: Iterable[NumericObservation],
) -> dict[tuple[object, ...], list[NumericObservation]]:
    grouped: dict[tuple[object, ...], list[NumericObservation]] = defaultdict(list)
    for item in observations:
        grouped[item.series_key].append(item)
    result: dict[tuple[object, ...], list[NumericObservation]] = {}
    for key, values in grouped.items():
        by_time: dict[datetime, list[NumericObservation]] = defaultdict(list)
        for item in values:
            by_time[item.timestamp].append(item)
        consolidated: list[NumericObservation] = []
        for timestamp, same_time in by_time.items():
            if len(same_time) == 1:
                consolidated.append(same_time[0])
                continue
            reference = same_time[0]
            consolidated.append(
                NumericObservation(
                    timestamp=timestamp,
                    source=reference.source,
                    node_id=reference.node_id,
                    related_node_ids=reference.related_node_ids,
                    metric=reference.metric,
                    value=float(median(item.value for item in same_time)),
                    dimensions=reference.dimensions,
                    direction=reference.direction,
                )
            )
        consolidated.sort(key=lambda item: item.timestamp)
        result[key] = consolidated
    return result


def _numeric_evidence(
    observations: Iterable[NumericObservation],
    config: dict[str, Any],
) -> list[AnomalyEvidence]:
    evidence: list[AnomalyEvidence] = []
    lookback = max(1, int(config.get("lookback_points", 60)))
    short_history = max(2, int(config.get("short_series_min_history", 4)))
    long_history = max(short_history, int(config.get("long_series_min_history", 15)))
    long_threshold = max(long_history, int(config.get("long_series_threshold", 60)))
    source_thresholds = config.get("source_thresholds", {})
    default_threshold = float(config.get("evidence_threshold", 6.0))

    def metric_config(metric: str) -> dict[str, Any]:
        absolute_floor = float(config.get("absolute_scale_floor", 0.01))
        for pattern, configured in config.get("metric_scale_floors", {}).items():
            if fnmatch(metric, pattern):
                absolute_floor = max(absolute_floor, float(configured))
        relative_floor = float(config.get("relative_scale_floor", 0.01))
        relative_overridden = False
        for pattern, configured in config.get("metric_relative_scale_floors", {}).items():
            if fnmatch(metric, pattern):
                relative_floor = float(configured)
                relative_overridden = True
        if (
            absolute_floor == float(config.get("absolute_scale_floor", 0.01))
            and not relative_overridden
        ):
            return config
        adjusted = dict(config)
        adjusted["absolute_scale_floor"] = absolute_floor
        adjusted["relative_scale_floor"] = relative_floor
        return adjusted

    for values in _consolidate_series(observations).values():
        minimum_history = long_history if len(values) >= long_threshold else short_history
        history: list[float] = []
        for item in values:
            if len(history) >= minimum_history:
                baseline, score = robust_score(
                    item.value,
                    history[-lookback:],
                    metric_config(item.metric),
                    direction=item.direction,
                )
                threshold = float(source_thresholds.get(item.source, default_threshold))
                if score >= threshold:
                    observed_direction = item.direction
                    if item.direction == "both":
                        observed_direction = "high" if item.value > baseline else "low"
                    evidence.append(
                        AnomalyEvidence(
                            timestamp=item.timestamp,
                            source=item.source,
                            node_id=item.node_id,
                            related_node_ids=item.related_node_ids,
                            metric=item.metric,
                            value=item.value,
                            baseline=baseline,
                            score=score,
                            direction=observed_direction,
                            dimensions=item.dimensions,
                            summary=None,
                        )
                    )
            history.append(item.value)
    return evidence


def _text_evidence(bundle: ObservationBundle, config: dict[str, Any]) -> list[AnomalyEvidence]:
    severity_scores = {
        "emerg": 25.0,
        "alert": 22.0,
        "crit": 18.0,
        "critical": 18.0,
        "err": 14.0,
        "error": 14.0,
        "warning": 9.0,
        "warn": 9.0,
        "notice": 6.0,
        "info": 4.0,
        "debug": 2.0,
        "unknown": 5.0,
    }
    threshold = float(config.get("source_thresholds", {}).get("frr", 5.0))
    result: list[AnomalyEvidence] = []
    for event in bundle.text_events:
        score = severity_scores.get(event.severity.lower(), 5.0)
        if event.source == "scrape":
            score = max(score, float(config.get("source_thresholds", {}).get("scrape", 7.0)))
        if score < threshold and event.source == "frr":
            continue
        result.append(
            AnomalyEvidence(
                timestamp=event.timestamp,
                source=event.source,
                node_id=event.node_id,
                related_node_ids=(),
                metric=f"{event.source}.{event.event_family}_event",
                value=1.0,
                baseline=0.0,
                score=min(float(config.get("max_score", 25.0)), score),
                direction="state",
                dimensions=(("program", event.program), ("severity", event.severity)),
                summary=event.message,
            )
        )
    return result


def _minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _minute_range(start: datetime, end: datetime) -> list[datetime]:
    values: list[datetime] = []
    current = _minute(start)
    final = _minute(end)
    while current <= final:
        values.append(current)
        current += timedelta(minutes=1)
    return values


def _energies(
    evidence: tuple[AnomalyEvidence, ...],
    minutes: list[datetime],
    config: dict[str, Any],
) -> tuple[dict[datetime, float], dict[datetime, dict[str, float]]]:
    by_minute_source: dict[datetime, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for point in evidence:
        by_minute_source[_minute(point.timestamp)][point.source].append(point.score)

    top_k = max(1, int(config.get("top_k_per_source", 3)))
    source_weights = config.get("source_weights", {})
    maximum = float(config.get("max_score", 25.0))
    minute_energy: dict[datetime, float] = {}
    source_energy: dict[datetime, dict[str, float]] = {}
    for minute in minutes:
        per_source: dict[str, float] = {}
        for source, scores in by_minute_source.get(minute, {}).items():
            strongest = sorted(scores, reverse=True)[:top_k]
            base = strongest[0]
            support = sum(strongest[1:]) / max(1, len(strongest) - 1)
            weight = float(source_weights.get(source, 1.0))
            per_source[source] = min(maximum, (base + 0.25 * support) * weight)
        ordered = sorted(per_source.values(), reverse=True)
        total = ordered[0] if ordered else 0.0
        if len(ordered) > 1:
            total += 0.25 * sum(ordered[1:]) / len(ordered[1:])
        minute_energy[minute] = min(maximum, total)
        source_energy[minute] = per_source
    return minute_energy, source_energy


def _windows_from_energy(
    minute_energy: dict[datetime, float],
    config: dict[str, Any],
) -> list[tuple[datetime, datetime]]:
    open_threshold = float(config.get("open_threshold", 7.0))
    keep_threshold = float(config.get("keep_threshold", 3.0))
    gap = max(0, int(config.get("gap_tolerance_minutes", 1)))
    padding = max(1, int(config.get("end_padding_minutes", 1)))
    windows: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    last_support: datetime | None = None
    for minute in sorted(minute_energy):
        energy = minute_energy[minute]
        if start is None:
            if energy >= open_threshold:
                start = minute
                last_support = minute
            continue
        if energy >= keep_threshold:
            last_support = minute
            continue
        assert last_support is not None
        if minute - last_support > timedelta(minutes=gap):
            windows.append((start, last_support + timedelta(minutes=padding)))
            start = None
            last_support = None
            if energy >= open_threshold:
                start = minute
                last_support = minute
    if start is not None and last_support is not None:
        windows.append((start, last_support + timedelta(minutes=padding)))
    return windows


def _split_window(
    start: datetime,
    end: datetime,
    minute_energy: dict[datetime, float],
    max_minutes: int,
) -> list[tuple[datetime, datetime]]:
    if end - start <= timedelta(minutes=max_minutes):
        return [(start, end)]
    candidates = [
        minute
        for minute in sorted(minute_energy)
        if start + timedelta(minutes=1) <= minute <= end - timedelta(minutes=1)
    ]
    if not candidates:
        split = start + timedelta(minutes=max_minutes)
    else:
        split = min(candidates, key=lambda minute: (minute_energy[minute], minute))
    return _split_window(start, split, minute_energy, max_minutes) + _split_window(
        split, end, minute_energy, max_minutes
    )


def _event_from_window(
    start: datetime,
    end: datetime,
    evidence: tuple[AnomalyEvidence, ...],
    minute_energy: dict[datetime, float],
    open_threshold: float,
    config: dict[str, Any],
) -> DetectedEvent | None:
    selected = tuple(point for point in evidence if start <= point.timestamp < end)
    if not selected:
        return None
    recovery = None
    if config.get("trim_recovery_reversals", True):
        recovery = _recovery_start(
            selected,
            start,
            float(config.get("recovery_reverse_threshold", 5.0)),
            max(2, int(config.get("recovery_min_initial_points", 3))),
        )
    if recovery is not None:
        end = recovery
        selected = tuple(point for point in selected if point.timestamp < end)
        if not selected:
            return None
    peak_time = max(
        (minute for minute in minute_energy if start <= minute < end),
        key=lambda minute: (minute_energy[minute], -minute.timestamp()),
    )
    counts = Counter(point.source for point in selected)
    confidence = min(1.0, minute_energy[peak_time] / max(open_threshold, 1e-9) / 1.5)
    return DetectedEvent(
        start=start,
        end=end,
        peak_time=peak_time,
        confidence=confidence,
        evidence=tuple(sorted(selected, key=lambda item: (-item.score, item.timestamp, item.metric))),
        source_counts=dict(sorted(counts.items())),
    )


def _recovery_start(
    points: tuple[AnomalyEvidence, ...],
    event_start: datetime,
    threshold: float,
    minimum_initial_points: int,
) -> datetime | None:
    """Find a sustained-series direction reversal caused by recovery.

    A rolling baseline can temporarily regard the return to normal as a second
    anomaly.  We only trim when a directly observed series has already supplied
    at least two same-direction points and then reverses strongly.
    """
    direct_sources = {"node", "interface", "routing", "frr"}
    initial: dict[tuple[object, ...], str] = {}
    counts: Counter[tuple[object, ...]] = Counter()
    by_minute: dict[datetime, list[AnomalyEvidence]] = defaultdict(list)
    for point in sorted(points, key=lambda item: (item.timestamp, -item.score)):
        by_minute[_minute(point.timestamp)].append(point)
    for minute in sorted(by_minute):
        reverse_score = 0.0
        for point in by_minute[minute]:
            if point.source not in direct_sources or point.direction not in {"high", "low"}:
                continue
            key = (point.source, point.node_id, point.metric, point.dimensions)
            first = initial.setdefault(key, point.direction)
            if point.direction == first:
                counts[key] += 1
            elif counts[key] >= minimum_initial_points:
                reverse_score += point.score
        if (
            minute >= event_start + timedelta(minutes=2)
            and reverse_score >= threshold
        ):
            return minute
    return None


def segment_evidence(
    evidence: Iterable[AnomalyEvidence],
    *,
    observation_start: datetime,
    observation_end: datetime,
    config: dict[str, Any],
    source_coverage: dict[str, int] | None = None,
) -> tuple[list[DetectedEvent], DetectionDiagnostics]:
    """Convert point evidence into non-overlapping, duration-bounded events."""
    points = tuple(sorted(evidence, key=lambda item: (item.timestamp, item.source, item.metric)))
    minutes = _minute_range(observation_start, observation_end)
    minute_energy, source_energy = _energies(points, minutes, config)
    raw_windows = _windows_from_energy(minute_energy, config)
    max_minutes = max(1, int(config.get("max_event_minutes", 30)))
    min_minutes = max(1, int(config.get("min_event_minutes", 1)))
    windows = [
        split
        for start, end in raw_windows
        for split in _split_window(start, end, minute_energy, max_minutes)
        if split[1] - split[0] >= timedelta(minutes=min_minutes)
    ]
    open_threshold = float(config.get("open_threshold", 7.0))
    events = [
        event
        for start, end in windows
        if (
            event := _event_from_window(
                start, end, points, minute_energy, open_threshold, config
            )
        )
        is not None
    ]
    diagnostics = DetectionDiagnostics(
        minute_energy=minute_energy,
        source_energy=source_energy,
        evidence_count=len(points),
        source_coverage=dict(source_coverage or {}),
        observation_start=observation_start,
        observation_end=observation_end,
        open_threshold=open_threshold,
        keep_threshold=float(config.get("keep_threshold", 3.0)),
    )
    return events, diagnostics


def detect_events(
    bundle: ObservationBundle,
    config: dict[str, Any],
) -> tuple[list[DetectedEvent], DetectionDiagnostics]:
    """Detect events from canonical numeric and textual observations."""
    all_times = [item.timestamp for item in bundle.numeric]
    all_times.extend(item.timestamp for item in bundle.text_events)
    if not all_times:
        diagnostics = DetectionDiagnostics(
            minute_energy={},
            source_energy={},
            evidence_count=0,
            source_coverage=dict(bundle.source_coverage),
            observation_start=None,
            observation_end=None,
            open_threshold=float(config.get("open_threshold", 7.0)),
            keep_threshold=float(config.get("keep_threshold", 3.0)),
        )
        return [], diagnostics
    points = _numeric_evidence(bundle.numeric, config)
    points.extend(_text_evidence(bundle, config))
    return segment_evidence(
        points,
        observation_start=min(all_times),
        observation_end=max(all_times),
        config=config,
        source_coverage=bundle.source_coverage,
    )

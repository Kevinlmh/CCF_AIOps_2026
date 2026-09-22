"""Robust multi-source anomaly scoring and bounded event segmentation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
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
    trigger_minute_energy: dict[datetime, float]
    source_energy: dict[datetime, dict[str, float]]
    evidence_count: int
    source_coverage: dict[str, int]
    observation_start: datetime | None
    observation_end: datetime | None
    open_threshold: float
    keep_threshold: float
    qualification_audit: tuple[dict[str, object], ...] = ()


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


def semantic_range_score(metric: str, value: float, config: dict[str, Any]) -> float:
    """Return a bounded physical-range severity independent of baseline drift."""
    for pattern, configured in config.get("metric_semantic_ranges", {}).items():
        if not fnmatch(metric, pattern):
            continue
        if "high_start" in configured and "high_full" in configured:
            start = float(configured["high_start"])
            full = float(configured["high_full"])
            if full <= start:
                raise ValueError(f"invalid high semantic range for metric pattern: {pattern}")
            return min(1.0, max(0.0, (value - start) / (full - start)))
        if "low_start" in configured and "low_full" in configured:
            start = float(configured["low_start"])
            full = float(configured["low_full"])
            if full >= start:
                raise ValueError(f"invalid low semantic range for metric pattern: {pattern}")
            return min(1.0, max(0.0, (start - value) / (start - full)))
        raise ValueError(f"semantic range requires high_* or low_* bounds: {pattern}")
    return 0.0


def evidence_event_role(
    item: NumericObservation,
    semantic_score: float,
    config: dict[str, Any],
) -> str:
    """Demote bounded metrics that are far from their hazardous range."""
    if item.event_role != "trigger":
        return item.event_role
    for pattern, configured in config.get(
        "trigger_semantic_minimums", {}
    ).items():
        if not fnmatch(item.metric, pattern):
            continue
        minimum = float(configured)
        if not 0.0 <= minimum <= 1.0:
            raise ValueError(
                f"invalid trigger semantic minimum for metric pattern: {pattern}"
            )
        return "trigger" if semantic_score >= minimum else "support"
    return item.event_role


def retain_evidence(point: AnomalyEvidence, config: dict[str, Any]) -> bool:
    """Return whether an evidence point carries non-trivial diagnostic value."""
    if point.event_role != "support" or point.value != 0.0:
        return True
    return not any(
        fnmatch(point.metric, pattern)
        for pattern in config.get("support_zero_suppression_patterns", ())
    )


def score_numeric_observation(
    item: NumericObservation,
    history: Iterable[float | None],
    config: dict[str, Any],
) -> tuple[float, float, str, float]:
    """Score one canonical observation, including known-normal state metrics."""
    if item.direction == "state" and item.normal_value is not None:
        tolerance = max(0.0, float(config.get("state_value_tolerance", 1e-9)))
        difference = abs(item.value - item.normal_value)
        score = float(config.get("max_score", 25.0)) if difference > tolerance else 0.0
        return item.normal_value, score, "state", 1.0 if score > 0 else 0.0
    baseline, score = robust_score(
        item.value,
        history,
        config,
        direction=item.direction,
    )
    observed_direction = item.direction
    if item.direction == "both":
        observed_direction = "high" if item.value > baseline else "low"
    return (
        baseline,
        score,
        observed_direction,
        semantic_range_score(item.metric, item.value, config),
    )


def _consolidate_series(
    observations: Iterable[NumericObservation],
    config: dict[str, Any] | None = None,
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
            sample_counts = [item.sample_count for item in same_time]
            numerator_counts = [item.numerator_count for item in same_time]
            has_complete_counts = all(
                value is not None for value in sample_counts + numerator_counts
            )
            sample_count = (
                sum(float(value) for value in sample_counts if value is not None)
                if has_complete_counts
                else None
            )
            numerator_count = (
                sum(float(value) for value in numerator_counts if value is not None)
                if has_complete_counts
                else None
            )
            value = float(median(item.value for item in same_time))
            if has_complete_counts and reference.metric.lower().endswith("_ratio"):
                prior_weight = max(
                    0.0,
                    float((config or {}).get("traffic_ratio_prior_weight", 0.0)),
                )
                assert sample_count is not None and numerator_count is not None
                effective_numerator = min(sample_count, numerator_count)
                prior_successes = (
                    prior_weight
                    if "success" in reference.metric.lower()
                    else 0.0
                )
                denominator = sample_count + prior_weight
                value = (
                    (effective_numerator + prior_successes) / denominator
                    if denominator > 0.0
                    else value
                )
            consolidated.append(
                NumericObservation(
                    timestamp=timestamp,
                    source=reference.source,
                    node_id=reference.node_id,
                    related_node_ids=tuple(
                        dict.fromkeys(
                            related
                            for item in same_time
                            for related in item.related_node_ids
                        )
                    ),
                    metric=reference.metric,
                    value=value,
                    dimensions=reference.dimensions,
                    direction=reference.direction,
                    event_role=reference.event_role,
                    normal_value=reference.normal_value,
                    sample_count=sample_count,
                    numerator_count=numerator_count,
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
    freeze_points = max(0, int(config.get("baseline_freeze_anomaly_points", 30)))

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

    for values in _consolidate_series(observations, config).values():
        minimum_history = long_history if len(values) >= long_threshold else short_history
        history: list[float] = []
        anomaly_run = 0
        for item in values:
            known_state = item.direction == "state" and item.normal_value is not None
            threshold = float(source_thresholds.get(item.source, default_threshold))
            score = 0.0
            if known_state or len(history) >= minimum_history:
                baseline, score, observed_direction, semantic_score = score_numeric_observation(
                    item,
                    history[-lookback:],
                    metric_config(item.metric),
                )
                if score >= threshold:
                    point = AnomalyEvidence(
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
                            sample_count=item.sample_count,
                            numerator_count=item.numerator_count,
                            event_role=evidence_event_role(
                                item, semantic_score, config
                            ),
                            semantic_score=semantic_score,
                        )
                    if retain_evidence(point, config):
                        evidence.append(point)
            if score >= threshold:
                anomaly_run += 1
                if anomaly_run > freeze_points:
                    history.append(item.value)
            else:
                anomaly_run = 0
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
    noise_patterns = tuple(
        str(pattern).lower() for pattern in config.get("frr_noise_patterns", [])
    )
    result: list[AnomalyEvidence] = []
    for event in bundle.text_events:
        if event.source == "frr" and any(
            pattern in event.message.lower() for pattern in noise_patterns
        ):
            continue
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
                event_role="trigger",
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


def _trigger_family(point: AnomalyEvidence) -> tuple[object, ...]:
    metric = point.metric.lower()
    parts = metric.split(".")
    if point.source == "node":
        if "cpu" in metric or ".load" in metric:
            return point.source, point.node_id, "resource_cpu"
        if "memory" in metric or "swap" in metric:
            return point.source, point.node_id, "resource_memory"
        if any(token in metric for token in ("disk_io", "disk_read", "disk_write")):
            return point.source, point.node_id, "resource_disk_io"
    if point.source == "traffic" and len(parts) >= 3:
        service = parts[1]
        suffix = ".".join(parts[2:])
        if "success" in suffix or "error" in suffix:
            family = "outcome"
        elif "latency_mean" in suffix or "latency_p95" in suffix:
            family = "latency"
        elif "observed_qps" in suffix or "throughput" in suffix:
            family = "availability"
        else:
            family = suffix
        return point.source, point.node_id, service, point.dimensions, family
    return point.source, point.node_id, point.metric


def _energies(
    evidence: tuple[AnomalyEvidence, ...],
    minutes: list[datetime],
    config: dict[str, Any],
) -> tuple[dict[datetime, float], dict[datetime, dict[str, float]]]:
    by_minute_source_family: dict[
        datetime, dict[str, dict[tuple[object, ...], float]]
    ] = defaultdict(lambda: defaultdict(dict))
    for point in evidence:
        families = by_minute_source_family[_minute(point.timestamp)][point.source]
        family = _trigger_family(point)
        families[family] = max(families.get(family, 0.0), point.score)

    top_k = max(1, int(config.get("top_k_per_source", 3)))
    source_weights = config.get("source_weights", {})
    maximum = float(config.get("max_score", 25.0))
    minute_energy: dict[datetime, float] = {}
    source_energy: dict[datetime, dict[str, float]] = {}
    for minute in minutes:
        per_source: dict[str, float] = {}
        for source, families in by_minute_source_family.get(minute, {}).items():
            scores = list(families.values())
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


def _traffic_window_qualifications(
    triggers: tuple[AnomalyEvidence, ...],
    config: dict[str, Any],
) -> tuple[set[AnomalyEvidence], list[dict[str, object]]]:
    """Confirm traffic-only incidents with sample-aware contiguous windows."""

    def runs(points: list[AnomalyEvidence]) -> list[list[AnomalyEvidence]]:
        ordered = sorted(points, key=lambda item: item.timestamp)
        result: list[list[AnomalyEvidence]] = []
        current: list[AnomalyEvidence] = []
        for point in ordered:
            if current and _minute(point.timestamp) != _minute(
                current[-1].timestamp
            ) + timedelta(minutes=1):
                result.append(current)
                current = []
            current.append(point)
        if current:
            result.append(current)
        return result

    def semantic_boundary(metric: str) -> tuple[str, float] | None:
        for pattern, configured in config.get("metric_semantic_ranges", {}).items():
            if not fnmatch(metric, pattern):
                continue
            if "high_start" in configured:
                return "high", float(configured["high_start"])
            if "low_start" in configured:
                return "low", float(configured["low_start"])
        return None

    traffic = tuple(point for point in triggers if point.source == "traffic")
    grouped: dict[tuple[object, ...], list[AnomalyEvidence]] = defaultdict(list)
    outcome_minutes: dict[tuple[object, ...], set[datetime]] = defaultdict(set)
    for point in traffic:
        parts = point.metric.lower().split(".")
        service = parts[1] if len(parts) >= 3 else point.metric.lower()
        scope = (point.node_id, service, point.dimensions)
        grouped[(point.node_id, point.metric, point.dimensions, point.direction)].append(
            point
        )
        if "success" in point.metric.lower() or "error" in point.metric.lower():
            outcome_minutes[scope].add(_minute(point.timestamp))

    qualified: set[AnomalyEvidence] = set()
    audit: list[dict[str, object]] = []
    ratio_minimum = max(2, int(config.get("traffic_ratio_window_min_minutes", 3)))
    sample_minimum = max(
        0.0, float(config.get("traffic_ratio_window_min_samples", 30.0))
    )
    prior_weight = max(0.0, float(config.get("traffic_ratio_prior_weight", 0.0)))
    latency_minimum = max(
        2, int(config.get("traffic_latency_window_min_minutes", 5))
    )
    correlated_latency_minimum = max(
        2, int(config.get("traffic_latency_correlated_min_minutes", 3))
    )

    for key, points in grouped.items():
        metric = str(key[1]).lower()
        parts = metric.split(".")
        service = parts[1] if len(parts) >= 3 else metric
        scope = (key[0], service, key[2])
        for run in runs(points):
            if metric.endswith("_ratio"):
                if len(run) < ratio_minimum:
                    continue
                if any(
                    point.sample_count is None or point.numerator_count is None
                    for point in run
                ):
                    continue
                sample_count = sum(float(point.sample_count) for point in run)
                numerator_count = sum(float(point.numerator_count) for point in run)
                if sample_count < sample_minimum:
                    continue
                success = "success" in metric
                effective_numerator = min(sample_count, numerator_count)
                posterior = (
                    effective_numerator + (prior_weight if success else 0.0)
                ) / (sample_count + prior_weight)
                boundary = semantic_boundary(metric)
                if boundary is None:
                    continue
                direction, threshold = boundary
                severe = posterior >= threshold if direction == "high" else posterior <= threshold
                if not severe:
                    continue
                qualified.update(run)
                audit.append(
                    {
                        "reason": "traffic_ratio_window",
                        "metric": key[1],
                        "start": _minute(run[0].timestamp),
                        "end": _minute(run[-1].timestamp),
                        "minutes": len(run),
                        "sample_count": sample_count,
                        "numerator_count": numerator_count,
                        "posterior_ratio": posterior,
                    }
                )
                continue

            if "latency" not in metric:
                continue
            run_minutes = {_minute(point.timestamp) for point in run}
            correlated = run_minutes & outcome_minutes.get(scope, set())
            minimum = (
                correlated_latency_minimum
                if len(correlated) >= correlated_latency_minimum
                else latency_minimum
            )
            if len(run) < minimum:
                continue
            qualified.update(run)
            audit.append(
                {
                    "reason": "traffic_latency_window",
                    "metric": key[1],
                    "start": _minute(run[0].timestamp),
                    "end": _minute(run[-1].timestamp),
                    "minutes": len(run),
                    "corroborated_minutes": len(correlated),
                }
            )
    audit.sort(key=lambda item: (item["start"], str(item["metric"])))
    return qualified, audit


def _qualified_trigger_evidence_with_audit(
    evidence: tuple[AnomalyEvidence, ...],
    config: dict[str, Any],
) -> tuple[tuple[AnomalyEvidence, ...], tuple[dict[str, object], ...]]:
    """Reject isolated gauge spikes while preserving state/log incidents.

    A gauge point becomes an event trigger when another independent trigger is
    present in the same minute or the same series persists in a neighboring
    minute. Support evidence is intentionally excluded from this decision.
    """
    triggers = tuple(point for point in evidence if point.event_role == "trigger")
    if not triggers:
        return (), ()
    required = max(1, int(config.get("min_distinct_trigger_series_per_minute", 2)))
    persistence_gap = max(0, int(config.get("trigger_persistence_gap_minutes", 1)))
    by_minute: dict[datetime, list[AnomalyEvidence]] = defaultdict(list)
    series_minutes: dict[tuple[object, ...], set[datetime]] = defaultdict(set)
    series_points: dict[
        tuple[object, ...], dict[datetime, AnomalyEvidence]
    ] = defaultdict(dict)

    def series_key(point: AnomalyEvidence) -> tuple[object, ...]:
        return (
            point.source,
            point.node_id,
            point.metric,
            point.dimensions,
            point.direction,
        )

    for point in triggers:
        minute = _minute(point.timestamp)
        by_minute[minute].append(point)
        series_minutes[series_key(point)].add(minute)
        series_points[series_key(point)][minute] = point

    qualified: list[AnomalyEvidence] = []
    minimum_persistent = max(2, int(config.get("min_persistent_trigger_minutes", 2)))
    metric_persistence = config.get("metric_min_persistent_trigger_minutes", {})
    require_cross = bool(config.get("corroboration_requires_cross_source_or_node", True))
    semantic_threshold = float(config.get("semantic_single_minute_threshold", 0.9))
    family_corroboration_sources = set(
        config.get("sources_require_independent_family_corroboration", ())
    )
    family_corroboration_minimum = max(
        1, int(config.get("source_family_corroboration_min_minutes", 1))
    )
    traffic_window_points, qualification_audit = _traffic_window_qualifications(
        triggers, config
    )
    grouped_families: dict[
        tuple[object, ...], dict[datetime, set[tuple[object, ...]]]
    ] = defaultdict(lambda: defaultdict(set))
    for point in triggers:
        if point.source not in family_corroboration_sources:
            continue
        family = _trigger_family(point)
        grouped_families[family[:-1]][_minute(point.timestamp)].add(family)

    def semantic_immediate_allowed(point: AnomalyEvidence) -> bool:
        if (
            point.source == "traffic"
            and point.metric.lower().endswith("_ratio")
            and "traffic_ratio_window_min_samples" in config
        ):
            if point.sample_count is None or point.numerator_count is None:
                return False
        for pattern, configured in config.get(
            "semantic_single_minimum_samples", {}
        ).items():
            if not fnmatch(point.metric, pattern):
                continue
            samples = point.sample_count
            if samples is None:
                return False
            return math.isfinite(samples) and samples >= float(configured)
        return True

    def consecutive_minutes(values: set[datetime], center: datetime) -> int:
        count = 1
        previous = center - timedelta(minutes=1)
        while previous in values:
            count += 1
            previous -= timedelta(minutes=1)
        following = center + timedelta(minutes=1)
        while following in values:
            count += 1
            following += timedelta(minutes=1)
        return count

    def trigger_tier(
        point: AnomalyEvidence,
    ) -> tuple[int | None, bool, float | None]:
        """Return required run length and whether persistence may qualify it."""
        for pattern, configured in config.get("metric_trigger_tiers", {}).items():
            if not fnmatch(point.metric, pattern):
                continue
            tiers = sorted(
                configured,
                key=lambda value: float(value["min_value"]),
                reverse=True,
            )
            for tier in tiers:
                if point.value >= float(tier["min_value"]):
                    return (
                        max(1, int(tier["min_persistent_minutes"])),
                        True,
                        float(tier["min_value"]),
                    )
            return None, False, None
        return None, True, None

    for minute, points in by_minute.items():
        for point in points:
            point_family = _trigger_family(point)
            corroborating_families = {point_family}
            for other in points:
                other_family = _trigger_family(other)
                if other is point or other_family == point_family:
                    continue
                if require_cross:
                    same_node_cross_source = (
                        point.node_id is not None
                        and point.node_id == other.node_id
                        and point.source != other.source
                    )
                    explicitly_related = (
                        other.node_id is not None
                        and other.node_id in point.related_node_ids
                    ) or (
                        point.node_id is not None
                        and point.node_id in other.related_node_ids
                    )
                    if not (same_node_cross_source or explicitly_related):
                        continue
                corroborating_families.add(other_family)
            corroborated = len(corroborating_families) >= required
            if point.source in family_corroboration_sources:
                family_minutes = {
                    candidate_minute
                    for candidate_minute, families in grouped_families[
                        point_family[:-1]
                    ].items()
                    if len(families) >= required
                }
                corroborated = (
                    minute in family_minutes
                    and consecutive_minutes(family_minutes, minute)
                    >= family_corroboration_minimum
                )
            key = series_key(point)
            persistent_minutes = consecutive_minutes(series_minutes[key], minute)
            point_minimum_persistent = minimum_persistent
            for pattern, configured in metric_persistence.items():
                if fnmatch(point.metric, pattern):
                    point_minimum_persistent = max(2, int(configured))
                    break
            tier_minimum, persistence_allowed, tier_value_minimum = trigger_tier(point)
            if tier_minimum is not None:
                point_minimum_persistent = tier_minimum
                tier_minutes = {
                    candidate_minute
                    for candidate_minute, candidate in series_points[key].items()
                    if candidate.value >= tier_value_minimum
                }
                persistent_minutes = consecutive_minutes(tier_minutes, minute)
            immediate = point.direction == "state" or point.source == "frr"
            persistent = (
                point.source not in family_corroboration_sources
                and persistence_allowed
                and persistent_minutes >= point_minimum_persistent
            )
            semantically_extreme = (
                point.semantic_score >= semantic_threshold
                and semantic_immediate_allowed(point)
            )
            if tier_minimum is not None:
                corroborated = False
            if not persistence_allowed:
                corroborated = False
                semantically_extreme = False
            traffic_window = point in traffic_window_points
            if immediate or corroborated or persistent or semantically_extreme or traffic_window:
                qualified.append(point)
    return (
        tuple(
            sorted(
                qualified,
                key=lambda item: (item.timestamp, item.source, item.metric, item.dimensions),
            )
        ),
        tuple(qualification_audit),
    )


def _qualified_trigger_evidence(
    evidence: tuple[AnomalyEvidence, ...],
    config: dict[str, Any],
) -> tuple[AnomalyEvidence, ...]:
    qualified, _ = _qualified_trigger_evidence_with_audit(evidence, config)
    return qualified


def _suppress_nearby_events(
    events: list[DetectedEvent],
    trigger_energy: dict[datetime, float],
    config: dict[str, Any],
) -> list[DetectedEvent]:
    """Keep the strongest peak when candidate incidents violate the gap prior."""
    separation = max(0, int(config.get("minimum_peak_separation_minutes", 0)))
    if separation == 0 or len(events) < 2:
        return events

    def quality(event: DetectedEvent) -> tuple[float, ...]:
        triggers = [
            point for point in event.evidence if point.event_role == "trigger"
        ]
        trigger_minutes = {
            point.timestamp.replace(second=0, microsecond=0) for point in triggers
        }
        family_minute_scores: dict[tuple[datetime, tuple[object, ...]], float] = {}
        for point in triggers:
            key = (_minute(point.timestamp), _trigger_family(point))
            family_minute_scores[key] = max(
                family_minute_scores.get(key, 0.0), point.score
            )
        return (
            float(any(point.direction == "state" or point.source == "frr" for point in triggers)),
            max((point.semantic_score for point in triggers), default=0.0),
            float(len(trigger_minutes)),
            float(len({point.source for point in triggers})),
            float(len({_trigger_family(point) for point in triggers})),
            sum(family_minute_scores.values()) / 25.0,
            trigger_energy.get(event.peak_time, 0.0),
            event.confidence,
            -event.peak_time.timestamp(),
        )

    selected: list[DetectedEvent] = []
    for event in sorted(events, key=quality, reverse=True):
        if any(
            abs((event.peak_time - kept.peak_time).total_seconds())
            < separation * 60
            for kept in selected
        ):
            continue
        selected.append(event)
    return sorted(selected, key=lambda item: (item.start, item.end, item.peak_time))


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
        preferred = min(start + timedelta(minutes=max_minutes), end - timedelta(minutes=1))
        split = min(
            candidates,
            key=lambda minute: (
                minute_energy[minute],
                abs((preferred - minute).total_seconds()),
                minute,
            ),
        )
    return _split_window(start, split, minute_energy, max_minutes) + _split_window(
        split, end, minute_energy, max_minutes
    )


def _event_from_window(
    start: datetime,
    end: datetime,
    evidence: tuple[AnomalyEvidence, ...],
    trigger_energy: dict[datetime, float],
    open_threshold: float,
    config: dict[str, Any],
) -> DetectedEvent | None:
    selected = tuple(point for point in evidence if start <= point.timestamp < end)
    if not selected:
        return None
    raw_triggers = tuple(
        point for point in selected if point.event_role == "trigger"
    )
    qualified_triggers = _qualified_trigger_evidence(raw_triggers, config)
    if not qualified_triggers:
        return None
    qualified_set = set(qualified_triggers)
    selected = tuple(
        replace(point, event_role="support")
        if point.event_role == "trigger" and point not in qualified_set
        else point
        for point in selected
    )
    local_minutes = [
        minute
        for minute in _minute_range(start, end)
        if start <= minute < end
    ]
    local_trigger_energy, _ = _energies(
        qualified_triggers, local_minutes, config
    )
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
        (minute for minute in local_trigger_energy if start <= minute < end),
        key=lambda minute: (local_trigger_energy[minute], -minute.timestamp()),
    )
    counts = Counter(point.source for point in selected)
    confidence = confidence_from_peak_energy(
        local_trigger_energy[peak_time], open_threshold
    )
    return DetectedEvent(
        start=start,
        end=end,
        peak_time=peak_time,
        confidence=confidence,
        evidence=tuple(sorted(selected, key=lambda item: (-item.score, item.timestamp, item.metric))),
        source_counts=dict(sorted(counts.items())),
    )


def confidence_from_peak_energy(peak_energy: float, open_threshold: float) -> float:
    """Map locally supported trigger energy to a bounded event confidence."""
    excess = max(0.0, peak_energy - open_threshold)
    return min(
        0.95,
        0.5 + 0.45 * (1.0 - math.exp(-excess / max(open_threshold, 1e-9))),
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
            if (
                point.event_role != "trigger"
                or point.source not in direct_sources
                or point.direction not in {"high", "low"}
            ):
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
    qualified_triggers, qualification_audit = _qualified_trigger_evidence_with_audit(
        points, config
    )
    trigger_minute_energy, _ = _energies(qualified_triggers, minutes, config)
    qualified_ids = {id(point) for point in qualified_triggers}
    event_points = tuple(
        replace(point, event_role="support")
        if point.event_role == "trigger" and id(point) not in qualified_ids
        else point
        for point in points
    )
    raw_windows = _windows_from_energy(trigger_minute_energy, config)
    max_minutes = max(1, int(config.get("max_event_minutes", 30)))
    min_minutes = max(1, int(config.get("min_event_minutes", 1)))
    windows = [
        split
        for start, end in raw_windows
        for split in _split_window(start, end, trigger_minute_energy, max_minutes)
        if split[1] - split[0] >= timedelta(minutes=min_minutes)
    ]
    open_threshold = float(config.get("open_threshold", 7.0))
    events = [
        event
        for start, end in windows
        if (
            event := _event_from_window(
                start,
                end,
                event_points,
                trigger_minute_energy,
                open_threshold,
                config,
            )
        )
        is not None
    ]
    events = _suppress_nearby_events(events, trigger_minute_energy, config)
    diagnostics = DetectionDiagnostics(
        minute_energy=minute_energy,
        trigger_minute_energy=trigger_minute_energy,
        source_energy=source_energy,
        evidence_count=len(points),
        source_coverage=dict(source_coverage or {}),
        observation_start=observation_start,
        observation_end=observation_end,
        open_threshold=open_threshold,
        keep_threshold=float(config.get("keep_threshold", 3.0)),
        qualification_audit=qualification_audit,
    )
    return events, diagnostics


def _point_city(point: AnomalyEvidence, cities: tuple[str, ...]) -> str | None:
    candidates = (point.node_id, *point.related_node_ids)
    for node_id in candidates:
        if node_id is None:
            continue
        lowered = node_id.lower()
        for city in cities:
            if lowered == city or lowered.startswith(city + "-"):
                return city
    return None


def segment_evidence_by_city(
    evidence: Iterable[AnomalyEvidence],
    *,
    cities: Iterable[str],
    observation_start: datetime,
    observation_end: datetime,
    config: dict[str, Any],
    source_coverage: dict[str, int] | None = None,
) -> tuple[list[DetectedEvent], DetectionDiagnostics]:
    """Generate incidents independently per city to prevent spatial pooling."""
    points = tuple(
        sorted(evidence, key=lambda item: (item.timestamp, item.source, item.metric))
    )
    city_ids = tuple(dict.fromkeys(str(city).lower() for city in cities))
    grouped: dict[str, list[AnomalyEvidence]] = defaultdict(list)
    for point in points:
        city = _point_city(point, city_ids)
        if city is not None:
            grouped[city].append(point)

    events: list[DetectedEvent] = []
    city_diagnostics: list[DetectionDiagnostics] = []
    for city in city_ids:
        city_events, city_diagnostic = segment_evidence(
            grouped.get(city, ()),
            observation_start=observation_start,
            observation_end=observation_end,
            config=config,
            source_coverage=source_coverage,
        )
        events.extend(city_events)
        city_diagnostics.append(city_diagnostic)

    minutes = _minute_range(observation_start, observation_end)
    minute_energy, source_energy = _energies(points, minutes, config)
    trigger_minute_energy = {
        minute: max(
            (item.trigger_minute_energy.get(minute, 0.0) for item in city_diagnostics),
            default=0.0,
        )
        for minute in minutes
    }
    diagnostics = DetectionDiagnostics(
        minute_energy=minute_energy,
        trigger_minute_energy=trigger_minute_energy,
        source_energy=source_energy,
        evidence_count=len(points),
        source_coverage=dict(source_coverage or {}),
        observation_start=observation_start,
        observation_end=observation_end,
        open_threshold=float(config.get("open_threshold", 7.0)),
        keep_threshold=float(config.get("keep_threshold", 3.0)),
        qualification_audit=tuple(
            audit
            for item in city_diagnostics
            for audit in item.qualification_audit
        ),
    )
    return sorted(events, key=lambda item: (item.start, item.end, item.peak_time)), diagnostics


def detect_events_with_evidence(
    bundle: ObservationBundle,
    config: dict[str, Any],
) -> tuple[list[DetectedEvent], DetectionDiagnostics, tuple[AnomalyEvidence, ...]]:
    """Detect events and retain the bounded pre-segmentation evidence."""
    all_times = [item.timestamp for item in bundle.numeric]
    all_times.extend(item.timestamp for item in bundle.text_events)
    if not all_times:
        diagnostics = DetectionDiagnostics(
            minute_energy={},
            trigger_minute_energy={},
            source_energy={},
            evidence_count=0,
            source_coverage=dict(bundle.source_coverage),
            observation_start=None,
            observation_end=None,
            open_threshold=float(config.get("open_threshold", 7.0)),
            keep_threshold=float(config.get("keep_threshold", 3.0)),
        )
        return [], diagnostics, ()
    points = _numeric_evidence(bundle.numeric, config)
    points.extend(_text_evidence(bundle, config))
    ordered_points = tuple(
        sorted(points, key=lambda item: (item.timestamp, item.source, item.metric))
    )
    events, diagnostics = segment_evidence(
        ordered_points,
        observation_start=min(all_times),
        observation_end=max(all_times),
        config=config,
        source_coverage=bundle.source_coverage,
    )
    return events, diagnostics, ordered_points


def detect_events(
    bundle: ObservationBundle,
    config: dict[str, Any],
) -> tuple[list[DetectedEvent], DetectionDiagnostics]:
    """Detect events from canonical numeric and textual observations."""
    events, diagnostics, _ = detect_events_with_evidence(bundle, config)
    return events, diagnostics

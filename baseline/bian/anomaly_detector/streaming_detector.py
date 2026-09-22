"""Bounded-state online robust anomaly detection for formal data."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from fnmatch import fnmatch
from typing import Any, Iterable

from .robust_detector import evidence_event_role, retain_evidence, score_numeric_observation
from ..preprocessing.observations import AnomalyEvidence, NumericObservation


class OnlineRobustDetector:
    """Score observations online while retaining only bounded history/evidence."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.lookback = max(1, int(config.get("lookback_points", 60)))
        self.short_history = max(2, int(config.get("short_series_min_history", 4)))
        self.long_history = max(
            self.short_history, int(config.get("long_series_min_history", 15))
        )
        self.long_threshold = max(
            self.long_history, int(config.get("long_series_threshold", 60))
        )
        self.maximum_per_bucket = max(
            1, int(config.get("max_evidence_per_minute_source_node", 8))
        )
        self._history: dict[tuple[object, ...], deque[float]] = {}
        self._counts: dict[tuple[object, ...], int] = defaultdict(int)
        self._undecided: dict[tuple[object, ...], list[NumericObservation]] = defaultdict(list)
        self._long_series: set[tuple[object, ...]] = set()
        self._anomaly_runs: dict[tuple[object, ...], int] = defaultdict(int)
        self._buckets: dict[tuple[object, ...], list[AnomalyEvidence]] = defaultdict(list)
        self._metric_config_cache: dict[str, dict[str, Any]] = {}
        self.observation_count = 0
        self.dropped_evidence_count = 0
        self._dropped_evidence_by_role: Counter[str] = Counter()
        self._finalized = False

    @property
    def series_state_count(self) -> int:
        return len(self._history)

    @property
    def retained_evidence_count(self) -> int:
        return sum(len(values) for values in self._buckets.values())

    @property
    def dropped_evidence_by_role(self) -> dict[str, int]:
        return dict(self._dropped_evidence_by_role)

    def _metric_config(self, metric: str) -> dict[str, Any]:
        cached = self._metric_config_cache.get(metric)
        if cached is not None:
            return cached
        absolute_floor = float(self.config.get("absolute_scale_floor", 0.01))
        for pattern, configured in self.config.get("metric_scale_floors", {}).items():
            if fnmatch(metric, pattern):
                absolute_floor = max(absolute_floor, float(configured))
        relative_floor = float(self.config.get("relative_scale_floor", 0.01))
        for pattern, configured in self.config.get(
            "metric_relative_scale_floors", {}
        ).items():
            if fnmatch(metric, pattern):
                relative_floor = float(configured)
        adjusted = dict(self.config)
        adjusted["absolute_scale_floor"] = absolute_floor
        adjusted["relative_scale_floor"] = relative_floor
        self._metric_config_cache[metric] = adjusted
        return adjusted

    def _retain(self, evidence: AnomalyEvidence) -> None:
        minute = evidence.timestamp.replace(second=0, microsecond=0)
        key = (minute, evidence.source, evidence.node_id)
        values = self._buckets[key]
        values.append(evidence)
        values.sort(
            key=lambda item: (
                item.event_role != "trigger",
                -item.score,
                item.timestamp,
                item.metric,
                item.dimensions,
            )
        )
        if len(values) > self.maximum_per_bucket:
            dropped = values[self.maximum_per_bucket :]
            del values[self.maximum_per_bucket :]
            self.dropped_evidence_count += len(dropped)
            self._dropped_evidence_by_role.update(
                item.event_role for item in dropped
            )

    def add_evidence(self, evidence: AnomalyEvidence) -> None:
        if self._finalized:
            raise RuntimeError("detector is already finalized")
        self._retain(evidence)

    def add(self, item: NumericObservation) -> None:
        if self._finalized:
            raise RuntimeError("detector is already finalized")
        self.observation_count += 1
        key = item.series_key
        self._counts[key] += 1
        known_state = item.direction == "state" and item.normal_value is not None
        if known_state:
            self._process(item, minimum_history=0)
            return
        if key in self._long_series:
            self._process(item, minimum_history=self.long_history)
            return
        buffered = self._undecided[key]
        buffered.append(item)
        if len(buffered) == self.long_threshold:
            self._long_series.add(key)
            del self._undecided[key]
            for pending in buffered:
                self._process(pending, minimum_history=self.long_history)

    def _process(self, item: NumericObservation, *, minimum_history: int) -> None:
        key = item.series_key
        history = self._history.setdefault(key, deque(maxlen=self.lookback))
        threshold = float(
            self.config.get("source_thresholds", {}).get(
                item.source, self.config.get("evidence_threshold", 6.0)
            )
        )
        evidence = None
        known_state = item.direction == "state" and item.normal_value is not None
        score = 0.0
        if known_state or len(history) >= minimum_history:
            baseline, score, observed_direction, semantic_score = score_numeric_observation(
                item,
                history,
                self._metric_config(item.metric),
            )
            if score >= threshold:
                evidence = AnomalyEvidence(
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
                    sample_count=item.sample_count,
                    numerator_count=item.numerator_count,
                    event_role=evidence_event_role(
                        item, semantic_score, self.config
                    ),
                    semantic_score=semantic_score,
                )

        if evidence is not None:
            self._anomaly_runs[key] += 1
            freeze_points = max(
                0, int(self.config.get("baseline_freeze_anomaly_points", 30))
            )
            if self._anomaly_runs[key] > freeze_points:
                history.append(item.value)
        else:
            self._anomaly_runs[key] = 0
            history.append(item.value)
        if evidence is not None:
            if retain_evidence(evidence, self.config):
                self._retain(evidence)

    def extend(self, observations: Iterable[NumericObservation]) -> None:
        for item in observations:
            self.add(item)

    def finalize(self) -> tuple[AnomalyEvidence, ...]:
        if not self._finalized:
            for pending in self._undecided.values():
                for item in pending:
                    self._process(item, minimum_history=self.short_history)
            self._undecided.clear()
            self._finalized = True
        return tuple(
            sorted(
                (item for values in self._buckets.values() for item in values),
                key=lambda item: (item.timestamp, item.source, item.node_id or "", -item.score, item.metric),
            )
        )

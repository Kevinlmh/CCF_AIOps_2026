"""Bounded-state online robust anomaly detection for formal data."""

from __future__ import annotations

from collections import defaultdict, deque
from fnmatch import fnmatch
from typing import Any, Iterable

from .robust_detector import robust_score
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
        self._pending_short: dict[tuple[object, ...], list[AnomalyEvidence]] = defaultdict(list)
        self._buckets: dict[tuple[object, ...], list[AnomalyEvidence]] = defaultdict(list)
        self._metric_config_cache: dict[str, dict[str, Any]] = {}
        self.observation_count = 0
        self.dropped_evidence_count = 0
        self._finalized = False

    @property
    def series_state_count(self) -> int:
        return len(self._history)

    @property
    def retained_evidence_count(self) -> int:
        return sum(len(values) for values in self._buckets.values())

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
        values.sort(key=lambda item: (-item.score, item.timestamp, item.metric, item.dimensions))
        if len(values) > self.maximum_per_bucket:
            del values[self.maximum_per_bucket :]
            self.dropped_evidence_count += 1

    def add_evidence(self, evidence: AnomalyEvidence) -> None:
        if self._finalized:
            raise RuntimeError("detector is already finalized")
        self._retain(evidence)

    def add(self, item: NumericObservation) -> None:
        if self._finalized:
            raise RuntimeError("detector is already finalized")
        self.observation_count += 1
        key = item.series_key
        history = self._history.setdefault(key, deque(maxlen=self.lookback))
        count_before = self._counts[key]
        threshold = float(
            self.config.get("source_thresholds", {}).get(
                item.source, self.config.get("evidence_threshold", 6.0)
            )
        )
        evidence = None
        if len(history) >= self.short_history:
            baseline, score = robust_score(
                item.value,
                history,
                self._metric_config(item.metric),
                direction=item.direction,
            )
            if score >= threshold:
                observed_direction = item.direction
                if item.direction == "both":
                    observed_direction = "high" if item.value > baseline else "low"
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
                    event_role=item.event_role,
                )

        history.append(item.value)
        self._counts[key] = count_before + 1
        if evidence is not None:
            if count_before < self.long_history:
                self._pending_short[key].append(evidence)
            else:
                self._retain(evidence)
        if self._counts[key] == self.long_threshold:
            self._pending_short.pop(key, None)

    def extend(self, observations: Iterable[NumericObservation]) -> None:
        for item in observations:
            self.add(item)

    def finalize(self) -> tuple[AnomalyEvidence, ...]:
        if not self._finalized:
            for key, pending in self._pending_short.items():
                if self._counts[key] < self.long_threshold:
                    for evidence in pending:
                        self._retain(evidence)
            self._pending_short.clear()
            self._finalized = True
        return tuple(
            sorted(
                (item for values in self._buckets.values() for item in values),
                key=lambda item: (item.timestamp, item.source, item.node_id or "", -item.score, item.metric),
            )
        )

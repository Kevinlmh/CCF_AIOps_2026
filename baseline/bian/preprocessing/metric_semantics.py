"""Configurable metric types and stateful counter conversion."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fnmatch import fnmatch
import json
from pathlib import Path

from .observations import NumericObservation


@dataclass(frozen=True, slots=True)
class MetricRule:
    kind: str
    direction: str
    normal_value: float | None = None
    scoring_direction: str | None = None
    detect: bool = True
    event_role: str = "trigger"


class MetricSemantics:
    def __init__(self, default: MetricRule, rules: tuple[tuple[str, MetricRule], ...]):
        self.default = default
        self.rules = rules

    @classmethod
    def from_json(cls, path: Path) -> "MetricSemantics":
        raw = json.loads(path.read_text(encoding="utf-8"))

        def rule(value: dict[str, object]) -> MetricRule:
            return MetricRule(
                kind=str(value.get("kind", "gauge")),
                direction=str(value.get("direction", "both")),
                normal_value=(
                    float(value["normal_value"])
                    if value.get("normal_value") is not None
                    else None
                ),
                scoring_direction=(
                    str(value["scoring_direction"])
                    if value.get("scoring_direction") is not None
                    else None
                ),
                detect=bool(value.get("detect", True)),
                event_role=str(value.get("event_role", "trigger")),
            )

        return cls(
            rule(raw.get("default", {})),
            tuple((str(item["pattern"]), rule(item)) for item in raw.get("rules", [])),
        )

    def rule_for(self, metric: str) -> MetricRule:
        for pattern, rule in self.rules:
            if fnmatch(metric, pattern):
                return rule
        return self.default


class CounterTransformer:
    """Apply configured direction and turn cumulative counters into rates."""

    def __init__(self, semantics: MetricSemantics):
        self.semantics = semantics
        self._previous: dict[tuple[object, ...], tuple[object, float]] = {}

    @staticmethod
    def _rate_name(metric: str) -> str:
        if metric.endswith("_total"):
            return metric[:-6] + "_rate"
        return metric + "_rate"

    def transform(self, item: NumericObservation) -> tuple[NumericObservation, ...]:
        rule = self.semantics.rule_for(item.metric)
        if not rule.detect:
            return ()
        if rule.event_role not in {"trigger", "support"}:
            raise ValueError(f"invalid event_role for metric {item.metric}: {rule.event_role}")
        scoring_direction = rule.scoring_direction or rule.direction
        directed = replace(
            item,
            direction=scoring_direction,
            event_role=rule.event_role,
        )
        if rule.kind != "counter":
            return (directed,)

        key = item.series_key
        previous = self._previous.get(key)
        self._previous[key] = (item.timestamp, item.value)
        if previous is None:
            return ()
        previous_time, previous_value = previous
        elapsed = (item.timestamp - previous_time).total_seconds() / 60.0
        if elapsed <= 0:
            return ()
        reset = item.value < previous_value
        delta = item.value if reset else item.value - previous_value
        rate = replace(
            directed,
            metric=self._rate_name(item.metric),
            value=delta / elapsed,
        )
        if not reset:
            return (rate,)
        marker = replace(
            directed,
            metric=item.metric.removesuffix("_total") + "_reset",
            value=1.0,
            direction="state",
        )
        return rate, marker

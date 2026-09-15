"""Configurable metric types and stateful counter conversion."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fnmatch import fnmatch
import json
from pathlib import Path
from typing import Iterable, Iterator

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

        def rule(value: dict[str, object], *, label: str) -> MetricRule:
            result = MetricRule(
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
            if result.kind not in {"gauge", "counter", "state", "context", "event"}:
                raise ValueError(f"invalid metric kind for {label}: {result.kind}")
            if result.direction not in {"high", "low", "both", "state"}:
                raise ValueError(f"invalid metric direction for {label}: {result.direction}")
            if result.scoring_direction not in {None, "high", "low", "both", "state"}:
                raise ValueError(
                    f"invalid metric scoring_direction for {label}: {result.scoring_direction}"
                )
            if result.event_role not in {"trigger", "support"}:
                raise ValueError(f"invalid metric event_role for {label}: {result.event_role}")
            return result

        parsed_rules: list[tuple[str, MetricRule]] = []
        patterns: set[str] = set()
        for index, item in enumerate(raw.get("rules", [])):
            pattern = str(item["pattern"])
            if pattern in patterns:
                raise ValueError(f"duplicate metric pattern: {pattern}")
            patterns.add(pattern)
            parsed_rules.append((pattern, rule(item, label=f"rules[{index}]")))

        return cls(
            rule(raw.get("default", {}), label="default"),
            tuple(parsed_rules),
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
        scoring_direction = (
            "state" if rule.kind == "state" else rule.scoring_direction or rule.direction
        )
        directed = replace(
            item,
            direction=scoring_direction,
            event_role=rule.event_role,
            normal_value=rule.normal_value,
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
            normal_value=0.0,
        )
        return rate, marker


def transform_observations(
    observations: Iterable[NumericObservation],
    semantics: MetricSemantics,
) -> Iterator[NumericObservation]:
    """Apply the same state/counter/role contract in memory and streaming modes."""
    transformer = CounterTransformer(semantics)
    for item in observations:
        yield from transformer.transform(item)

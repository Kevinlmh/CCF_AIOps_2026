from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from baseline.bian.preprocessing.metric_semantics import (
    CounterTransformer,
    MetricSemantics,
)
from baseline.bian.preprocessing.observations import NumericObservation


CONFIG = Path(__file__).parents[1] / "baseline" / "bian" / "config" / "metric_semantics.json"
UTC = timezone.utc


def observation(minute: int, metric: str, value: float) -> NumericObservation:
    return NumericObservation(
        timestamp=datetime(2026, 8, 19, 4, 0, tzinfo=UTC) + timedelta(minutes=minute),
        source=metric.split(".", 1)[0],
        node_id="beida-br-1",
        related_node_ids=(),
        metric=metric,
        value=value,
        dimensions=(),
    )


class MetricSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.semantics = MetricSemantics.from_json(CONFIG)

    def test_known_metric_rules_define_kind_and_direction(self):
        self.assertEqual(self.semantics.rule_for("node.cpu_usage").direction, "high")
        self.assertEqual(
            self.semantics.rule_for("node.memory_available_ratio").direction, "low"
        )
        carrier = self.semantics.rule_for("interface.carrier_changes")
        self.assertEqual((carrier.kind, carrier.direction), ("counter", "high"))
        peer = self.semantics.rule_for("routing.bgp_peer_up")
        self.assertEqual((peer.kind, peer.direction, peer.normal_value), ("state", "low", 1.0))

    def test_counter_uses_actual_elapsed_minutes_and_marks_reset(self):
        transformer = CounterTransformer(self.semantics)

        self.assertEqual(transformer.transform(observation(0, "interface.carrier_changes", 10)), ())
        delta = transformer.transform(observation(2, "interface.carrier_changes", 14))
        reset = transformer.transform(observation(3, "interface.carrier_changes", 1))

        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0].metric, "interface.carrier_changes_rate")
        self.assertEqual(delta[0].value, 2.0)
        self.assertEqual(delta[0].direction, "high")
        self.assertEqual({item.metric for item in reset}, {
            "interface.carrier_changes_rate",
            "interface.carrier_changes_reset",
        })
        self.assertEqual(next(item for item in reset if item.metric.endswith("_rate")).value, 1.0)

    def test_gauge_keeps_value_and_applies_configured_direction(self):
        transformer = CounterTransformer(self.semantics)
        result = transformer.transform(observation(0, "node.cpu_usage", 91.0))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].value, 91.0)
        self.assertEqual(result[0].direction, "both")


if __name__ == "__main__":
    unittest.main()

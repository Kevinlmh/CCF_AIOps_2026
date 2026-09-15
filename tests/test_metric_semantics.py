from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from baseline.bian.preprocessing.metric_semantics import (
    CounterTransformer,
    MetricSemantics,
)
from baseline.bian.preprocessing import metric_semantics as metric_semantics_module
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

    def test_workload_scheduler_fields_do_not_enter_anomaly_detection(self):
        transformer = CounterTransformer(self.semantics)

        self.assertEqual(
            transformer.transform(observation(0, "traffic.dns.active", 1.0)),
            (),
        )
        self.assertEqual(
            transformer.transform(
                observation(0, "traffic.web.batch_concurrency", 4.0)
            ),
            (),
        )

    def test_volume_metrics_are_marked_as_support_not_event_triggers(self):
        transformer = CounterTransformer(self.semantics)

        interface = transformer.transform(
            observation(0, "interface.rx_bytes_rate", 4096.0)
        )
        netflow = transformer.transform(observation(0, "netflow.bytes", 8192.0))

        self.assertEqual(interface[0].event_role, "support")
        self.assertEqual(netflow[0].event_role, "support")

    def test_latency_histogram_sum_and_count_rates_are_support_only(self):
        transformer = CounterTransformer(self.semantics)

        total = transformer.transform(
            observation(0, "traffic.web.request_latency_seconds_sum_rate", 12.0)
        )
        count = transformer.transform(
            observation(0, "traffic.web.request_latency_seconds_count_rate", 30.0)
        )

        self.assertEqual(total[0].event_role, "support")
        self.assertEqual(count[0].event_role, "support")

    def test_duplicate_metric_patterns_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "semantics.json"
            path.write_text(
                '{"default":{},"rules":['
                '{"pattern":"traffic.*.*throughput*"},'
                '{"pattern":"traffic.*.*throughput*","event_role":"support"}'
                "]}",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate metric pattern"):
                MetricSemantics.from_json(path)

    def test_known_state_carries_normal_value_and_state_direction(self):
        result = CounterTransformer(self.semantics).transform(
            observation(0, "routing.bgp_peer_up", 0.0)
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].direction, "state")
        self.assertEqual(result[0].normal_value, 1.0)

    def test_configuration_dependent_state_does_not_assume_enabled_is_normal(self):
        enabled = CounterTransformer(self.semantics).transform(
            observation(0, "routing.ospf6_interface_enabled", 0.0)
        )
        route = CounterTransformer(self.semantics).transform(
            observation(0, "routing.ipv6_route_exists", 0.0)
        )

        self.assertEqual(enabled[0].direction, "state")
        self.assertIsNone(enabled[0].normal_value)
        self.assertEqual(route[0].direction, "state")
        self.assertIsNone(route[0].normal_value)

    def test_throughput_is_support_only(self):
        result = CounterTransformer(self.semantics).transform(
            observation(0, "traffic.web.throughput_bps", 0.0)
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].event_role, "support")

    def test_observed_qps_is_support_only(self):
        result = CounterTransformer(self.semantics).transform(
            observation(0, "traffic.web.observed_qps", 5.0)
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].event_role, "support")

    def test_load_average_is_support_only(self):
        result = CounterTransformer(self.semantics).transform(
            observation(0, "node.load5", 2.0)
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].event_role, "support")

    def test_shared_transform_applies_counter_and_role_semantics_in_order(self):
        transformed = tuple(
            metric_semantics_module.transform_observations(
                (
                    observation(0, "interface.carrier_changes", 10.0),
                    observation(1, "interface.carrier_changes", 12.0),
                    observation(1, "traffic.web.throughput_bps", 1000.0),
                ),
                self.semantics,
            )
        )

        self.assertEqual(
            [(item.metric, item.event_role) for item in transformed],
            [
                ("interface.carrier_changes_rate", "trigger"),
                ("traffic.web.throughput_bps", "support"),
            ],
        )


if __name__ == "__main__":
    unittest.main()

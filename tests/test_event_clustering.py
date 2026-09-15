from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import unittest

from baseline.bian.anomaly_detector.event_clustering import split_concurrent_events
from baseline.bian.preprocessing.observations import AnomalyEvidence, DetectedEvent


UTC = timezone.utc
START = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)


def evidence(
    city: str,
    minute: int,
    *,
    related: tuple[str, ...] = (),
    event_role: str = "trigger",
    score: float = 10.0,
    source: str | None = None,
) -> AnomalyEvidence:
    resolved_source = source or ("traffic" if related else "node")
    return AnomalyEvidence(
        timestamp=START + timedelta(minutes=minute),
        source=resolved_source,
        node_id=(
            f"{city}-traffic-vm"
            if resolved_source == "traffic"
            else f"{city}-service-vm-1"
        ),
        related_node_ids=related,
        metric=(
            "traffic.web.error_ratio"
            if resolved_source == "traffic"
            else "node.cpu_usage"
        ),
        value=10.0,
        baseline=1.0,
        score=score,
        direction="high",
        dimensions=(),
        event_role=event_role,
    )


def event(*points: AnomalyEvidence) -> DetectedEvent:
    return DetectedEvent(
        start=START,
        end=START + timedelta(minutes=5),
        peak_time=START + timedelta(minutes=1),
        confidence=0.8,
        evidence=points,
        source_counts={"node": len(points)},
    )


class EventClusteringTests(unittest.TestCase):
    def test_unrelated_cities_in_same_window_split_into_two_events(self):
        original = event(evidence("beida", 1), evidence("wuhan", 2))

        result = split_concurrent_events([original], ("beida", "wuhan"))

        self.assertEqual(len(result), 2)
        self.assertEqual(
            {item.evidence[0].node_id.split("-", 1)[0] for item in result},
            {"beida", "wuhan"},
        )

    def test_cross_city_relational_evidence_keeps_cities_together(self):
        bridge = evidence(
            "beida", 1, related=("wuhan-service-vm-1", "wuhan-service-vm-2")
        )
        original = event(bridge, evidence("wuhan", 2))

        result = split_concurrent_events([original], ("beida", "wuhan"))

        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].evidence), 2)

    def test_support_relation_does_not_join_independent_trigger_cities(self):
        support_bridge = evidence(
            "beida",
            1,
            related=("wuhan-service-vm-1",),
            event_role="support",
        )
        original = event(
            evidence("beida", 1),
            support_bridge,
            evidence("wuhan", 2),
        )

        result = split_concurrent_events([original], ("beida", "wuhan"))

        self.assertEqual(len(result), 2)

    def test_support_only_spatial_child_is_rejected(self):
        original = event(
            evidence("beida", 1),
            evidence("wuhan", 2, event_role="support"),
        )

        result = split_concurrent_events([original], ("beida", "wuhan"))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].evidence[0].node_id, "beida-service-vm-1")

    def test_spatial_child_preserves_parent_window_for_root_padding(self):
        original = event(evidence("beida", 2), evidence("wuhan", 3))

        result = split_concurrent_events([original], ("beida", "wuhan"))

        self.assertEqual(len(result), 2)
        self.assertTrue(all(item.start == original.start for item in result))
        self.assertTrue(all(item.end == original.end for item in result))

    def test_split_children_recompute_confidence_from_local_trigger_energy(self):
        original = event(
            evidence("beida", 1, score=10.0),
            evidence("wuhan", 2, score=20.0),
        )
        config = {
            "open_threshold": 7.0,
            "max_score": 25.0,
            "min_distinct_trigger_series_per_minute": 1,
        }

        result = split_concurrent_events(
            [original], ("beida", "wuhan"), config=config
        )

        by_city = {item.evidence[0].node_id.split("-", 1)[0]: item for item in result}
        expected_beida = 0.5 + 0.45 * (1.0 - math.exp(-3.0 / 7.0))
        expected_wuhan = 0.5 + 0.45 * (1.0 - math.exp(-13.0 / 7.0))
        self.assertAlmostEqual(by_city["beida"].confidence, expected_beida)
        self.assertAlmostEqual(by_city["wuhan"].confidence, expected_wuhan)

    def test_cross_city_pooled_subthreshold_triggers_are_rejected_after_split(self):
        original = event(
            evidence("beida", 1, score=6.0, source="node"),
            evidence("wuhan", 1, score=6.0, source="traffic"),
        )
        config = {
            "open_threshold": 7.0,
            "max_score": 25.0,
            "source_weights": {"node": 1.0, "traffic": 1.1},
            "min_distinct_trigger_series_per_minute": 1,
        }

        result = split_concurrent_events(
            [original], ("beida", "wuhan"), config=config
        )

        self.assertEqual(result, [])

    def test_spatial_child_must_requalify_its_own_trigger_persistence(self):
        original = event(
            evidence("beida", 1),
            evidence("beida", 2),
            evidence("wuhan", 1),
            evidence("wuhan", 2),
            evidence("wuhan", 3),
        )
        config = {
            "open_threshold": 7.0,
            "max_score": 25.0,
            "trigger_persistence_gap_minutes": 1,
            "min_persistent_trigger_minutes": 2,
            "metric_min_persistent_trigger_minutes": {"node.cpu_usage": 3},
        }

        result = split_concurrent_events(
            [original], ("beida", "wuhan"), config=config
        )

        self.assertEqual(len(result), 1)
        self.assertTrue(
            all(point.node_id.startswith("wuhan-") for point in result[0].evidence)
        )


if __name__ == "__main__":
    unittest.main()

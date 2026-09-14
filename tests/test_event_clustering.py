from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from baseline.bian.anomaly_detector.event_clustering import split_concurrent_events
from baseline.bian.preprocessing.observations import AnomalyEvidence, DetectedEvent


UTC = timezone.utc
START = datetime(2026, 8, 19, 4, 0, tzinfo=UTC)


def evidence(city: str, minute: int, *, related: tuple[str, ...] = ()) -> AnomalyEvidence:
    return AnomalyEvidence(
        timestamp=START + timedelta(minutes=minute),
        source="traffic" if related else "node",
        node_id=f"{city}-traffic-vm" if related else f"{city}-service-vm-1",
        related_node_ids=related,
        metric="traffic.web.error_ratio" if related else "node.cpu_usage",
        value=10.0,
        baseline=1.0,
        score=10.0,
        direction="high",
        dimensions=(),
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


if __name__ == "__main__":
    unittest.main()

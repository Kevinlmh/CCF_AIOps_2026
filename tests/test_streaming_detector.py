from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from baseline.bian.anomaly_detector.robust_detector import _numeric_evidence
from baseline.bian.anomaly_detector.streaming_detector import OnlineRobustDetector
from baseline.bian.preprocessing.observations import NumericObservation


UTC = timezone.utc
CONFIG = {
    "lookback_points": 60,
    "short_series_min_history": 4,
    "long_series_min_history": 15,
    "long_series_threshold": 60,
    "absolute_scale_floor": 0.1,
    "relative_scale_floor": 0.0,
    "metric_scale_floors": {},
    "metric_relative_scale_floors": {},
    "source_thresholds": {"node": 5.0},
    "evidence_threshold": 6.0,
    "max_score": 25.0,
    "max_evidence_per_minute_source_node": 2,
}


def point(
    minute: int,
    value: float,
    metric: str = "node.cpu_usage",
    *,
    event_role: str = "trigger",
) -> NumericObservation:
    return NumericObservation(
        timestamp=datetime(2026, 8, 19, 4, 0, tzinfo=UTC) + timedelta(minutes=minute),
        source="node",
        node_id="beida-service-vm-1",
        related_node_ids=(),
        metric=metric,
        value=value,
        dimensions=(),
        direction="high",
        event_role=event_role,
    )


class StreamingDetectorTests(unittest.TestCase):
    def test_observation_event_role_is_preserved_in_emitted_evidence(self):
        detector = OnlineRobustDetector(CONFIG)
        detector.extend(
            [point(i, 10.0, "netflow.bytes", event_role="support") for i in range(4)]
        )
        detector.add(point(4, 20.0, "netflow.bytes", event_role="support"))

        evidence = detector.finalize()

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].event_role, "support")

    def test_long_series_matches_offline_warmup_and_scores(self):
        observations = [point(i, 10.0) for i in range(65)]
        observations[20] = point(20, 20.0)
        observations[64] = point(64, 30.0)

        detector = OnlineRobustDetector(CONFIG)
        detector.extend(observations)
        actual = detector.finalize()
        expected = _numeric_evidence(observations, CONFIG)

        self.assertEqual(
            [(item.timestamp, item.metric, item.score) for item in actual],
            [(item.timestamp, item.metric, item.score) for item in expected],
        )

    def test_short_series_releases_anomaly_after_final_length_is_known(self):
        detector = OnlineRobustDetector(CONFIG)
        detector.extend([point(i, 10.0) for i in range(4)])
        detector.add(point(4, 20.0))

        self.assertEqual(detector.retained_evidence_count, 0)
        evidence = detector.finalize()

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].timestamp.minute, 4)

    def test_evidence_is_bounded_per_minute_source_and_node(self):
        detector = OnlineRobustDetector(CONFIG)
        for metric_index in range(5):
            metric = f"node.metric_{metric_index}"
            detector.extend([point(i, 10.0, metric) for i in range(15)])
            detector.add(point(15, 10.6 + 0.1 * metric_index, metric))

        evidence = detector.finalize()

        self.assertEqual(len(evidence), 2)
        self.assertEqual([item.value for item in evidence], [11.0, 10.9])
        self.assertLessEqual(detector.series_state_count, 5)


if __name__ == "__main__":
    unittest.main()

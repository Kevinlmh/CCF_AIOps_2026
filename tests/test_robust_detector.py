from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import unittest

from baseline.bian.anomaly_detector.robust_detector import (
    detect_events,
    robust_score,
    segment_evidence,
)
from baseline.bian.preprocessing.multisource import ObservationBundle
from baseline.bian.preprocessing.observations import (
    AnomalyEvidence,
    NumericObservation,
    ParseStats,
)


BASE = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
CONFIG = {
    "lookback_points": 60,
    "short_series_min_history": 4,
    "long_series_min_history": 15,
    "long_series_threshold": 60,
    "absolute_scale_floor": 0.01,
    "relative_scale_floor": 0.01,
    "evidence_threshold": 5.0,
    "source_thresholds": {},
    "max_score": 25.0,
    "top_k_per_source": 3,
    "open_threshold": 7.0,
    "keep_threshold": 3.0,
    "gap_tolerance_minutes": 1,
    "end_padding_minutes": 1,
    "min_event_minutes": 1,
    "max_event_minutes": 30,
}


def observation(minute: int, value: float, *, direction: str = "high") -> NumericObservation:
    return NumericObservation(
        timestamp=BASE + timedelta(minutes=minute),
        source="node",
        node_id="xian-service-vm-1",
        related_node_ids=(),
        metric="node.cpu_usage",
        value=value,
        dimensions=(),
        direction=direction,
    )


def evidence(minute: int, score: float = 10.0) -> AnomalyEvidence:
    return AnomalyEvidence(
        timestamp=BASE + timedelta(minutes=minute),
        source="node",
        node_id="xian-service-vm-1",
        related_node_ids=(),
        metric="node.cpu_usage",
        value=80.0,
        baseline=1.0,
        score=score,
        direction="high",
        dimensions=(),
        summary=None,
    )


class RobustScoreTests(unittest.TestCase):
    def test_zero_variance_history_produces_finite_bounded_score(self):
        baseline, score = robust_score(10.0, [0.0, 0.0, 0.0, 0.0], CONFIG)

        self.assertEqual(baseline, 0.0)
        self.assertTrue(math.isfinite(score))
        self.assertEqual(score, 25.0)

    def test_direction_suppresses_change_in_non_anomalous_direction(self):
        history = [10.0, 10.0, 10.0, 10.0]

        self.assertEqual(robust_score(1.0, history, CONFIG, direction="high")[1], 0.0)
        self.assertEqual(robust_score(20.0, history, CONFIG, direction="low")[1], 0.0)
        self.assertGreater(robust_score(1.0, history, CONFIG, direction="low")[1], 5.0)
        self.assertGreater(robust_score(20.0, history, CONFIG, direction="high")[1], 5.0)

    def test_missing_and_non_finite_history_values_are_ignored(self):
        baseline, score = robust_score(
            10.0,
            [1.0, None, float("nan"), 1.0, 1.0, 1.0],
            CONFIG,
        )

        self.assertEqual(baseline, 1.0)
        self.assertEqual(score, 25.0)


class EventSegmentationTests(unittest.TestCase):
    def test_short_internal_gap_is_bridged_and_quiet_period_separates_events(self):
        points = tuple(
            evidence(minute)
            for minute in (4, 5, 7, 8, 35, 36, 37)
        )

        events, diagnostics = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=40),
            config=CONFIG,
        )

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].start, BASE + timedelta(minutes=4))
        self.assertEqual(events[0].end, BASE + timedelta(minutes=9))
        self.assertEqual(events[1].start, BASE + timedelta(minutes=35))
        self.assertEqual(events[1].end, BASE + timedelta(minutes=38))
        self.assertEqual(diagnostics.minute_energy[BASE + timedelta(minutes=6)], 0.0)

    def test_event_over_thirty_minutes_splits_at_lowest_internal_energy(self):
        points = tuple(
            evidence(minute, score=7.1 if minute == 15 else 12.0)
            for minute in range(31)
        )

        events, _ = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=31),
            config=CONFIG,
        )

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].end, BASE + timedelta(minutes=15))
        self.assertEqual(events[1].start, BASE + timedelta(minutes=15))
        self.assertTrue(all((item.end - item.start) <= timedelta(minutes=30) for item in events))

    def test_detect_events_scores_series_after_warmup(self):
        values = [1.0] * 4 + [20.0, 20.0, 1.0, 20.0, 20.0] + [1.0] * 26 + [20.0] * 3
        bundle = ObservationBundle(
            numeric=tuple(observation(index, value) for index, value in enumerate(values)),
            text_events=(),
            stats=ParseStats(),
            source_coverage={"node": len(values)},
        )

        events, diagnostics = detect_events(bundle, CONFIG)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].start, BASE + timedelta(minutes=4))
        self.assertGreater(diagnostics.evidence_count, 0)
        self.assertEqual(diagnostics.source_coverage, {"node": len(values)})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import unittest

from baseline.bian.anomaly_detector.robust_detector import (
    _text_evidence,
    detect_events,
    robust_score,
    segment_evidence,
)
from baseline.bian.preprocessing.multisource import ObservationBundle
from baseline.bian.preprocessing.observations import (
    AnomalyEvidence,
    NumericObservation,
    ParseStats,
    TextEvent,
)


BASE = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
CONFIG = {
    "lookback_points": 60,
    "short_series_min_history": 4,
    "long_series_min_history": 15,
    "long_series_threshold": 60,
    "absolute_scale_floor": 0.01,
    "relative_scale_floor": 0.01,
    "metric_scale_floors": {
        "node.cpu_usage": 1.0,
        "interface.*_bytes_rate": 1000.0,
    },
    "metric_relative_scale_floors": {
        "node.memory_available_ratio": 0.0,
    },
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


def evidence(
    minute: int,
    score: float = 10.0,
    *,
    direction: str = "high",
    event_role: str = "trigger",
    metric: str = "node.cpu_usage",
    source: str = "node",
) -> AnomalyEvidence:
    return AnomalyEvidence(
        timestamp=BASE + timedelta(minutes=minute),
        source=source,
        node_id="xian-service-vm-1",
        related_node_ids=(),
        metric=metric,
        value=80.0,
        baseline=1.0,
        score=score,
        direction=direction,
        dimensions=(),
        summary=None,
        event_role=event_role,
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


class TextEvidenceTests(unittest.TestCase):
    def test_configured_frr_noise_is_suppressed_but_fault_log_is_retained(self):
        events = (
            TextEvent(
                timestamp=BASE,
                source="frr",
                node_id="xian-br-1",
                severity="err",
                program="ospf6d",
                event_family="ospf",
                message="sendmsg failed: Operation not permitted",
            ),
            TextEvent(
                timestamp=BASE + timedelta(minutes=1),
                source="frr",
                node_id="xian-br-1",
                severity="err",
                program="bgpd",
                event_family="bgp",
                message="BGP peer went Down",
            ),
        )
        bundle = ObservationBundle((), events, ParseStats(), {"frr": 2})
        config = {**CONFIG, "frr_noise_patterns": ["sendmsg failed", "operation not permitted"]}

        result = _text_evidence(bundle, config)

        self.assertEqual(len(result), 1)
        self.assertIn("BGP peer", result[0].summary)


class EventSegmentationTests(unittest.TestCase):
    def test_support_only_anomalies_cannot_open_an_event(self):
        points = tuple(
            evidence(
                minute,
                score=25.0,
                event_role="support",
                metric="netflow.bytes",
            )
            for minute in range(4, 10)
        )

        events, diagnostics = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=12),
            config=CONFIG,
        )

        self.assertEqual(events, [])
        self.assertGreater(diagnostics.minute_energy[BASE + timedelta(minutes=5)], 0.0)
        self.assertEqual(
            diagnostics.trigger_minute_energy[BASE + timedelta(minutes=5)], 0.0
        )

    def test_trigger_opens_event_and_support_inside_window_is_retained(self):
        points = (
            evidence(4),
            evidence(5),
            evidence(
                5,
                score=25.0,
                event_role="support",
                metric="netflow.bytes",
            ),
        )

        events, _ = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config=CONFIG,
        )

        self.assertEqual(len(events), 1)
        self.assertTrue(any(item.event_role == "support" for item in events[0].evidence))

    def test_isolated_continuous_gauge_spike_requires_corroboration(self):
        events, _ = segment_evidence(
            (evidence(4, score=25.0),),
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config=CONFIG,
        )

        self.assertEqual(events, [])

    def test_correlated_service_outcome_derivatives_count_as_one_trigger_family(self):
        points = (
            evidence(
                4,
                score=25.0,
                metric="traffic.web.success_ratio",
                source="traffic",
                direction="low",
            ),
            evidence(
                4,
                score=25.0,
                metric="traffic.web.error_ratio",
                source="traffic",
            ),
            evidence(
                4,
                score=25.0,
                metric="traffic.web.error_rate",
                source="traffic",
            ),
        )

        events, _ = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config=CONFIG,
        )

        self.assertEqual(events, [])

    def test_state_change_can_open_a_single_minute_event(self):
        events, _ = segment_evidence(
            (evidence(4, score=12.0, direction="state", metric="routing.bgp_peer_up"),),
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config=CONFIG,
        )

        self.assertEqual(len(events), 1)

    def test_close_candidate_peaks_keep_only_strongest_event(self):
        config = {**CONFIG, "minimum_peak_separation_minutes": 20}
        points = (
            evidence(4, score=10.0),
            evidence(5, score=10.0),
            evidence(14, score=20.0),
            evidence(15, score=20.0),
        )

        events, _ = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=20),
            config=config,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].peak_time, BASE + timedelta(minutes=14))

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

    def test_metric_scale_floor_suppresses_tiny_sparse_interface_toggles(self):
        interface = tuple(
            NumericObservation(
                timestamp=BASE + timedelta(minutes=index),
                source="interface",
                node_id="xian-service-vm-2",
                related_node_ids=(),
                metric="interface.tx_bytes_rate",
                value=value,
                dimensions=(("interface_id", "ens3"),),
                direction="both",
            )
            for index, value in enumerate([60.0] * 4 + [0.0] * 5)
        )
        cpu = tuple(observation(index, value) for index, value in enumerate([1.0] * 4 + [40.0] * 5))
        bundle = ObservationBundle(
            numeric=interface + cpu,
            text_events=(),
            stats=ParseStats(),
            source_coverage={"node": 9, "interface": 9},
        )

        events, _ = detect_events(bundle, CONFIG)

        self.assertEqual(len(events), 1)
        self.assertTrue(any(item.metric == "node.cpu_usage" for item in events[0].evidence))
        self.assertFalse(any(item.metric == "interface.tx_bytes_rate" for item in events[0].evidence))

    def test_metric_relative_floor_preserves_meaningful_ratio_drop(self):
        memory = tuple(
            NumericObservation(
                timestamp=BASE + timedelta(minutes=index),
                source="node",
                node_id="xian-service-vm-3",
                related_node_ids=(),
                metric="node.memory_available_ratio",
                value=value,
                dimensions=(),
                direction="low",
            )
            for index, value in enumerate([0.924, 0.923, 0.925, 0.923, 0.855, 0.852, 0.851, 0.850])
        )
        bundle = ObservationBundle(
            numeric=memory,
            text_events=(),
            stats=ParseStats(),
            source_coverage={"node": len(memory)},
        )

        config = dict(CONFIG)
        config["relative_scale_floor"] = 0.1
        events, _ = detect_events(bundle, config)

        self.assertEqual(len(events), 1)
        self.assertTrue(
            any(item.metric == "node.memory_available_ratio" for item in events[0].evidence)
        )

    def test_opposite_direction_recovery_closes_event_before_recovery_tail(self):
        points = tuple(evidence(minute, direction="high") for minute in range(4, 9)) + tuple(
            evidence(minute, direction="low") for minute in range(9, 14)
        )

        events, _ = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=14),
            config=CONFIG,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].start, BASE + timedelta(minutes=4))
        self.assertEqual(events[0].end, BASE + timedelta(minutes=9))
        self.assertTrue(all(item.direction == "high" for item in events[0].evidence))

    def test_support_direction_reversal_does_not_trim_trigger_event(self):
        triggers = tuple(evidence(minute) for minute in range(4, 9))
        support = tuple(
            evidence(
                minute,
                direction="high" if minute < 7 else "low",
                event_role="support",
                metric="node.disk_write_rate",
            )
            for minute in range(4, 9)
        )

        events, _ = segment_evidence(
            triggers + support,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=10),
            config=CONFIG,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].end, BASE + timedelta(minutes=9))


if __name__ == "__main__":
    unittest.main()

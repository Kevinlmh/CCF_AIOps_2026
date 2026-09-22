from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import unittest

from baseline.bian.anomaly_detector import robust_detector as robust_module
from baseline.bian.anomaly_detector.robust_detector import (
    _consolidate_series,
    _energies,
    _qualified_trigger_evidence,
    _numeric_evidence,
    _text_evidence,
    detect_events,
    robust_score,
    segment_evidence,
    segment_evidence_by_city,
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
    semantic_score: float = 0.0,
    node_id: str = "xian-service-vm-1",
    related_node_ids: tuple[str, ...] = (),
    value: float = 80.0,
    dimensions: tuple[tuple[str, str], ...] = (),
    sample_count: float | None = None,
    numerator_count: float | None = None,
) -> AnomalyEvidence:
    return AnomalyEvidence(
        timestamp=BASE + timedelta(minutes=minute),
        source=source,
        node_id=node_id,
        related_node_ids=related_node_ids,
        metric=metric,
        value=value,
        baseline=1.0,
        score=score,
        direction=direction,
        dimensions=dimensions,
        summary=None,
        event_role=event_role,
        semantic_score=semantic_score,
        sample_count=sample_count,
        numerator_count=numerator_count,
    )


class RobustScoreTests(unittest.TestCase):
    def test_duplicate_ratio_timestamp_aggregates_counts_and_recomputes_posterior(self):
        common = {
            "timestamp": BASE,
            "source": "traffic",
            "node_id": "xian-traffic-vm",
            "related_node_ids": (),
            "metric": "traffic.web.error_ratio",
            "dimensions": (("domain", "a.example"),),
            "direction": "high",
        }
        observations = (
            NumericObservation(
                **common,
                value=10.0 / 40.0,
                sample_count=10.0,
                numerator_count=10.0,
            ),
            NumericObservation(
                **common,
                value=0.0,
                sample_count=90.0,
                numerator_count=0.0,
            ),
        )

        consolidated = _consolidate_series(
            observations, {"traffic_ratio_prior_weight": 30.0}
        )
        point = next(iter(consolidated.values()))[0]

        self.assertEqual(point.sample_count, 100.0)
        self.assertEqual(point.numerator_count, 10.0)
        self.assertAlmostEqual(point.value, 10.0 / 130.0)

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
    def test_energy_counts_correlated_service_outcomes_once_per_source(self):
        minute = BASE + timedelta(minutes=4)
        points = (
            evidence(4, score=25.0, metric="traffic.web.success_ratio", source="traffic"),
            evidence(4, score=20.0, metric="traffic.web.error_ratio", source="traffic"),
            evidence(4, score=18.0, metric="traffic.web.error_rate", source="traffic"),
        )

        energy, per_source = _energies(points, [minute], CONFIG)

        self.assertEqual(per_source[minute]["traffic"], 25.0)
        self.assertEqual(energy[minute], 25.0)

    def test_city_local_segmentation_prevents_cross_city_corroboration(self):
        points = (
            evidence(
                4,
                metric="node.cpu_usage",
                node_id="xian-service-vm-1",
                related_node_ids=("beida-service-vm-1",),
            ),
            evidence(
                4,
                metric="node.disk_io_util",
                node_id="beida-service-vm-1",
                related_node_ids=("xian-service-vm-1",),
            ),
        )

        events, _ = segment_evidence_by_city(
            points,
            cities=("xian", "beida"),
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config=CONFIG,
        )

        self.assertEqual(events, [])

    def test_cpu_trigger_tiers_use_absolute_value_and_persistence(self):
        config = {
            **CONFIG,
            "metric_trigger_tiers": {
                "node.cpu_usage": [
                    {"min_value": 35.0, "min_persistent_minutes": 3},
                    {"min_value": 20.0, "min_persistent_minutes": 5},
                ]
            },
        }

        low, _ = segment_evidence(
            tuple(evidence(minute, value=10.0) for minute in range(4, 10)),
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=12),
            config=config,
        )
        medium_short, _ = segment_evidence(
            tuple(evidence(minute, value=25.0) for minute in range(4, 8)),
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=12),
            config=config,
        )
        medium_long, _ = segment_evidence(
            tuple(evidence(minute, value=25.0) for minute in range(4, 9)),
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=12),
            config=config,
        )
        high, _ = segment_evidence(
            tuple(evidence(minute, value=40.0) for minute in range(4, 7)),
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=12),
            config=config,
        )

        self.assertEqual(low, [])
        self.assertEqual(medium_short, [])
        self.assertEqual(len(medium_long), 1)
        self.assertEqual(len(high), 1)

    def test_below_minimum_cpu_is_not_rescued_by_related_traffic_node(self):
        config = {
            **CONFIG,
            "metric_trigger_tiers": {
                "node.cpu_usage": [
                    {"min_value": 20.0, "min_persistent_minutes": 5}
                ]
            },
        }
        cpu = evidence(
            4,
            value=10.0,
            node_id="xian-service-vm-1",
            related_node_ids=("xian-traffic-vm",),
        )
        traffic = evidence(
            4,
            source="traffic",
            metric="traffic.web.error_ratio",
            node_id="xian-traffic-vm",
            related_node_ids=("xian-service-vm-1",),
        )

        qualified = _qualified_trigger_evidence((cpu, traffic), config)

        self.assertNotIn(cpu, qualified)

    def test_traffic_symptom_needs_two_families_unless_semantically_extreme(self):
        config = {
            **CONFIG,
            "sources_require_independent_family_corroboration": ["traffic"],
        }
        outcome = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.error_ratio",
                node_id="xian-traffic-vm",
                semantic_score=0.3,
            )
            for minute in range(4, 9)
        )
        latency = evidence(
            4,
            source="traffic",
            metric="traffic.web.latency_p95_seconds",
            node_id="xian-traffic-vm",
        )

        outcome_only = _qualified_trigger_evidence(outcome, config)
        corroborated = _qualified_trigger_evidence(outcome + (latency,), config)
        extreme = _qualified_trigger_evidence(
            (
                evidence(
                    4,
                    source="traffic",
                    metric="traffic.web.error_ratio",
                    node_id="xian-traffic-vm",
                    semantic_score=1.0,
                ),
            ),
            config,
        )

        self.assertEqual(outcome_only, ())
        self.assertIn(outcome[0], corroborated)
        self.assertEqual(len(extreme), 1)

    def _traffic_window_config(self):
        return {
            **CONFIG,
            "sources_require_independent_family_corroboration": ["traffic"],
            "source_family_corroboration_min_minutes": 3,
            "traffic_ratio_prior_weight": 30.0,
            "traffic_ratio_window_min_minutes": 3,
            "traffic_ratio_window_min_samples": 30.0,
            "traffic_latency_window_min_minutes": 5,
            "traffic_latency_correlated_min_minutes": 3,
            "metric_semantic_ranges": {
                "traffic.*.*success*_ratio": {"low_start": 0.9, "low_full": 0.5},
                "traffic.*.*error*_ratio": {"high_start": 0.1, "high_full": 0.5},
            },
            "semantic_single_minimum_samples": {"traffic.*.*_ratio": 30},
            "semantic_single_minute_threshold": 0.9,
        }

    def test_three_tiny_sample_ratio_minutes_do_not_qualify(self):
        points = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.error_ratio",
                node_id="xian-traffic-vm",
                semantic_score=0.3,
                value=2.0 / 34.0,
                sample_count=4.0,
                numerator_count=2.0,
            )
            for minute in range(4, 7)
        )

        self.assertEqual(
            _qualified_trigger_evidence(points, self._traffic_window_config()), ()
        )

    def test_ratio_missing_numerator_cannot_bypass_v16_gate_as_extreme(self):
        point = evidence(
            4,
            source="traffic",
            metric="traffic.web.error_ratio",
            node_id="xian-traffic-vm",
            semantic_score=1.0,
            sample_count=100.0,
            numerator_count=None,
        )

        self.assertEqual(
            _qualified_trigger_evidence(
                (point,), self._traffic_window_config()
            ),
            (),
        )

    def test_traffic_families_with_different_dimensions_do_not_corroborate(self):
        ratio = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.error_ratio",
                node_id="xian-traffic-vm",
                dimensions=(("domain", "a.example"),),
                semantic_score=0.3,
                value=2.0 / 34.0,
                sample_count=4.0,
                numerator_count=2.0,
            )
            for minute in range(4, 7)
        )
        latency = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.latency_p95_seconds",
                node_id="xian-traffic-vm",
                dimensions=(("domain", "b.example"),),
                semantic_score=0.3,
            )
            for minute in range(4, 7)
        )

        self.assertEqual(
            _qualified_trigger_evidence(
                ratio + latency, self._traffic_window_config()
            ),
            (),
        )

    def test_time_split_requalifies_children_without_discarding_long_ratio_incident(self):
        config = {
            **self._traffic_window_config(),
            "minimum_peak_separation_minutes": 30,
            "max_event_minutes": 30,
        }
        points = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.error_ratio",
                node_id="xian-traffic-vm",
                semantic_score=0.3,
                value=1.0,
                sample_count=29.0 if minute == 0 else 12.0,
                numerator_count=29.0 if minute == 0 else 12.0,
            )
            for minute in range(31)
        )

        events, _ = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=32),
            config=config,
        )

        self.assertEqual(len(events), 1)
        self.assertGreaterEqual(len(events[0].evidence), 30)
        self.assertGreaterEqual(
            events[0].end - events[0].start, timedelta(minutes=29)
        )

    def test_time_split_rejects_ratio_remainder_that_cannot_qualify_alone(self):
        config = {
            **self._traffic_window_config(),
            "minimum_peak_separation_minutes": 0,
            "max_event_minutes": 3,
        }
        points = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.error_ratio",
                node_id="xian-traffic-vm",
                semantic_score=0.3,
                value=10.0 / 40.0,
                sample_count=10.0,
                numerator_count=10.0,
            )
            for minute in range(4)
        )

        events, _ = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=5),
            config=config,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0].evidence), 3)

    def test_low_volume_ratio_qualifies_after_window_accumulates_samples(self):
        points = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.error_ratio",
                node_id="xian-traffic-vm",
                semantic_score=0.3,
                value=6.0 / 42.0,
                sample_count=12.0,
                numerator_count=6.0,
            )
            for minute in range(4, 7)
        )

        qualified = _qualified_trigger_evidence(points, self._traffic_window_config())

        self.assertEqual(qualified, points)

    def test_ratio_window_audit_recomputes_posterior_from_aggregated_counts(self):
        points = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.error_ratio",
                node_id="xian-traffic-vm",
                semantic_score=0.3,
                value=6.0 / 50.0,
                sample_count=20.0,
                numerator_count=6.0,
            )
            for minute in range(4, 7)
        )

        qualified, audit = robust_module._qualified_trigger_evidence_with_audit(
            points, self._traffic_window_config()
        )

        self.assertEqual(qualified, points)
        self.assertEqual(len(audit), 1)
        self.assertAlmostEqual(audit[0]["posterior_ratio"], 0.2)
        self.assertEqual(audit[0]["sample_count"], 60.0)
        self.assertEqual(audit[0]["numerator_count"], 18.0)

    def test_pure_latency_requires_five_minutes(self):
        config = self._traffic_window_config()
        points = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.latency_p95_seconds",
                node_id="xian-traffic-vm",
                semantic_score=0.0,
                value=2.0,
            )
            for minute in range(4, 9)
        )

        self.assertEqual(_qualified_trigger_evidence(points[:4], config), ())
        self.assertEqual(_qualified_trigger_evidence(points, config), points)

    def test_latency_with_outcome_corroboration_qualifies_in_three_minutes(self):
        config = self._traffic_window_config()
        latency = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.latency_p95_seconds",
                node_id="xian-traffic-vm",
                value=2.0,
            )
            for minute in range(4, 7)
        )
        outcome = tuple(
            evidence(
                minute,
                source="traffic",
                metric="traffic.web.error_ratio",
                node_id="xian-traffic-vm",
                value=6.0 / 42.0,
                semantic_score=0.3,
                sample_count=12.0,
                numerator_count=6.0,
            )
            for minute in range(4, 7)
        )

        qualified = _qualified_trigger_evidence(latency + outcome, config)

        self.assertEqual(set(qualified), set(latency + outcome))

    def test_immediate_state_does_not_increase_trigger_energy_with_unrelated_gauge(self):
        minute = BASE + timedelta(minutes=4)
        points = (
            evidence(4, score=12.0, direction="state", metric="routing.bgp_peer_up"),
            evidence(4, score=25.0, metric="node.disk_io_util"),
        )

        events, diagnostics = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config=CONFIG,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(diagnostics.trigger_minute_energy[minute], 12.0)
        disk = next(
            point
            for point in events[0].evidence
            if point.metric == "node.disk_io_util"
        )
        self.assertEqual(disk.event_role, "support")

    def test_cpu_and_load_are_one_causal_family(self):
        points = (
            evidence(4, score=25.0, metric="node.cpu_usage"),
            evidence(4, score=25.0, metric="node.load1"),
        )

        events, _ = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config=CONFIG,
        )

        self.assertEqual(events, [])

    def test_extreme_semantic_position_can_open_one_minute_gauge_event(self):
        events, _ = segment_evidence(
            (evidence(4, score=9.0, semantic_score=1.0),),
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config={**CONFIG, "semantic_single_minute_threshold": 0.9},
        )

        self.assertEqual(len(events), 1)

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

    def test_metric_specific_persistence_rejects_two_minute_low_range_cpu_burst(self):
        events, _ = segment_evidence(
            (evidence(4, score=10.0), evidence(5, score=10.0)),
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config={
                **CONFIG,
                "metric_min_persistent_trigger_minutes": {"node.cpu_usage": 3},
            },
        )

        self.assertEqual(events, [])

    def test_metric_specific_persistence_keeps_three_minute_cpu_incident(self):
        events, _ = segment_evidence(
            tuple(evidence(minute, score=10.0) for minute in (4, 5, 6)),
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=9),
            config={
                **CONFIG,
                "metric_min_persistent_trigger_minutes": {"node.cpu_usage": 3},
            },
        )

        self.assertEqual(len(events), 1)

    def test_recovery_direction_does_not_satisfy_trigger_persistence(self):
        points = (
            evidence(4, value=40.0, direction="high"),
            evidence(5, value=40.0, direction="high"),
            evidence(6, value=20.0, direction="low"),
        )

        events, _ = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config={
                **CONFIG,
                "metric_trigger_tiers": {
                    "node.cpu_usage": [
                        {"min_value": 35.0, "min_persistent_minutes": 3}
                    ]
                },
            },
        )

        self.assertEqual(events, [])

    def test_values_below_cpu_tier_do_not_count_toward_tier_persistence(self):
        points = tuple(
            evidence(minute, value=value, direction="high")
            for minute, value in enumerate((8.0, 12.0, 40.0), start=4)
        )

        events, _ = segment_evidence(
            points,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=8),
            config={
                **CONFIG,
                "metric_trigger_tiers": {
                    "node.cpu_usage": [
                        {"min_value": 35.0, "min_persistent_minutes": 3}
                    ]
                },
            },
        )

        self.assertEqual(events, [])

    def test_unrelated_cross_node_spikes_do_not_corroborate_each_other(self):
        points = (
            evidence(4, metric="node.cpu_usage", node_id="xian-service-vm-1"),
            evidence(4, metric="node.disk_io_util", node_id="beida-service-vm-1"),
        )

        events, _ = segment_evidence(
            points,
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

    def test_nearby_event_quality_prefers_sustained_signal_over_derivative_volume(self):
        config = {**CONFIG, "minimum_peak_separation_minutes": 20}
        noisy = tuple(
            evidence(
                minute,
                score=25.0,
                metric=f"node.metric_{metric_index}",
            )
            for minute in (4, 5)
            for metric_index in range(8)
        )
        sustained = tuple(
            evidence(minute, score=12.0, metric="node.cpu_usage")
            for minute in (14, 15, 16, 17)
        )

        events, _ = segment_evidence(
            noisy + sustained,
            observation_start=BASE,
            observation_end=BASE + timedelta(minutes=22),
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

    def test_semantic_range_is_recorded_as_independent_evidence_dimension(self):
        values = tuple(observation(index, value) for index, value in enumerate([1.0] * 15 + [90.0]))
        config = {
            **CONFIG,
            "metric_semantic_ranges": {
                "node.cpu_usage": {"high_start": 70.0, "high_full": 90.0}
            },
        }

        points = _numeric_evidence(values, config)

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].semantic_score, 1.0)

    def test_low_semantic_disk_util_anomaly_is_demoted_to_support(self):
        values = tuple(
            NumericObservation(
                timestamp=BASE + timedelta(minutes=index),
                source="node",
                node_id="xian-service-vm-1",
                related_node_ids=(),
                metric="node.disk_io_util",
                value=value,
                dimensions=(),
                direction="high",
            )
            for index, value in enumerate([1.0] * 15 + [20.0])
        )
        config = {
            **CONFIG,
            "metric_semantic_ranges": {
                "node.disk_io_util": {"high_start": 70.0, "high_full": 90.0}
            },
            "trigger_semantic_minimums": {"node.disk_io_util": 0.1},
        }

        points = _numeric_evidence(values, config)

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].event_role, "support")

    def test_configured_zero_support_metric_is_not_retained_as_evidence(self):
        values = tuple(
            NumericObservation(
                timestamp=BASE + timedelta(minutes=index),
                source="node",
                node_id="xian-service-vm-1",
                related_node_ids=(),
                metric="node.disk_write_rate",
                value=value,
                dimensions=(),
                direction="both",
                event_role="support",
            )
            for index, value in enumerate([100.0] * 4 + [0.0])
        )

        points = _numeric_evidence(
            values,
            {
                **CONFIG,
                "support_zero_suppression_patterns": ["node.disk_*_rate"],
            },
        )

        self.assertEqual(points, [])

    def test_high_semantic_disk_util_anomaly_remains_a_trigger(self):
        values = tuple(
            NumericObservation(
                timestamp=BASE + timedelta(minutes=index),
                source="node",
                node_id="xian-service-vm-1",
                related_node_ids=(),
                metric="node.disk_io_util",
                value=value,
                dimensions=(),
                direction="high",
            )
            for index, value in enumerate([1.0] * 15 + [80.0])
        )
        config = {
            **CONFIG,
            "metric_semantic_ranges": {
                "node.disk_io_util": {"high_start": 70.0, "high_full": 90.0}
            },
            "trigger_semantic_minimums": {"node.disk_io_util": 0.1},
        }

        points = _numeric_evidence(values, config)

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].event_role, "trigger")

    def test_short_history_fault_does_not_contaminate_frozen_baseline(self):
        values = tuple(
            observation(index, value)
            for index, value in enumerate([1.0] * 15 + [100.0] * 30)
        )

        points = _numeric_evidence(
            values,
            {**CONFIG, "baseline_freeze_anomaly_points": 30},
        )

        self.assertEqual(len([item for item in points if item.value == 100.0]), 30)

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

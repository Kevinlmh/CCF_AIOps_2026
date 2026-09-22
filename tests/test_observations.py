from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
import unittest

from baseline.bian.preprocessing.observations import (
    AnomalyEvidence,
    DetectedEvent,
    NumericObservation,
    ParseStats,
    city_from_path,
    normalize_node_id,
    parse_time,
)


VALID_ROLES = (
    "br-1",
    "br-2",
    "cr-1",
    "cr-2",
    "fw",
    "traffic-vm",
    "service-vm-1",
    "service-vm-2",
    "service-vm-3",
    "monitor-vm",
)


class ObservationContractTests(unittest.TestCase):
    def test_parse_time_normalizes_zoned_and_naive_values_to_utc(self):
        zoned = parse_time("2026-07-28T20:39:34+08:00")
        naive = parse_time("2026-07-28 12:39:34")

        self.assertEqual(zoned, datetime(2026, 7, 28, 12, 39, 34, tzinfo=timezone.utc))
        self.assertEqual(naive, datetime(2026, 7, 28, 12, 39, 34, tzinfo=timezone.utc))
        self.assertIsNone(parse_time("not-a-time"))

    def test_city_from_path_prefers_path_component_aliases(self):
        aliases = {"beida": "beida", "xian": "xian"}
        path = Path("case/xian_20260728/processed/node_metrics.csv")

        self.assertEqual(city_from_path(path, aliases), "xian")
        self.assertIsNone(city_from_path(Path("case/unknown/processed/data.csv"), aliases))

    def test_normalize_router_service_and_monitor_ids(self):
        self.assertEqual(
            normalize_node_id("BR-1-ccf-aiops-西安", "xian", VALID_ROLES),
            "xian-br-1",
        )
        self.assertEqual(
            normalize_node_id("service-vm-1", "xian", VALID_ROLES),
            "xian-service-vm-1",
        )
        self.assertEqual(
            normalize_node_id("monitor-vm", "xian", VALID_ROLES),
            "xian-monitor-vm",
        )
        self.assertIsNone(normalize_node_id("probe-vm", "xian", VALID_ROLES))
        self.assertIsNone(normalize_node_id("br-1", None, VALID_ROLES))

    def test_numeric_observation_series_key_preserves_dimensions(self):
        observation = NumericObservation(
            timestamp=datetime(2026, 7, 28, 12, 40, tzinfo=timezone.utc),
            source="interface",
            node_id="xian-br-1",
            related_node_ids=(),
            metric="interface.rx_drop_rate",
            value=3.0,
            dimensions=(("if_role", "uplink"), ("interface_id", "ens4")),
            direction="high",
        )

        self.assertEqual(
            observation.series_key,
            (
                "interface",
                "xian-br-1",
                "interface.rx_drop_rate",
                (("if_role", "uplink"), ("interface_id", "ens4")),
            ),
        )

    def test_ratio_counts_do_not_change_series_identity(self):
        common = {
            "timestamp": datetime(2026, 7, 28, 12, 40, tzinfo=timezone.utc),
            "source": "traffic",
            "node_id": "xian-traffic-vm",
            "related_node_ids": ("shanghai-service-vm-1",),
            "metric": "traffic.web.error_ratio",
            "value": 0.2,
            "dimensions": (("series_key", "flow-a"),),
            "direction": "high",
        }

        first = NumericObservation(
            **common, sample_count=10.0, numerator_count=2.0
        )
        second = NumericObservation(
            **common, sample_count=100.0, numerator_count=20.0
        )

        self.assertEqual(first.series_key, second.series_key)

    def test_ratio_counts_must_be_finite_non_negative_and_bounded(self):
        common = {
            "timestamp": datetime(2026, 7, 28, 12, 40, tzinfo=timezone.utc),
            "source": "traffic",
            "node_id": "xian-traffic-vm",
            "related_node_ids": (),
            "metric": "traffic.web.error_ratio",
            "value": 0.2,
            "dimensions": (),
            "direction": "high",
            "sample_count": 10.0,
        }
        for invalid in (float("nan"), -1.0, 11.0):
            with self.subTest(numerator_count=invalid), self.assertRaises(ValueError):
                NumericObservation(**common, numerator_count=invalid)

    def test_numeric_observation_rejects_non_finite_value(self):
        with self.assertRaises(ValueError):
            NumericObservation(
                timestamp=datetime(2026, 7, 28, 12, 40, tzinfo=timezone.utc),
                source="node",
                node_id="xian-service-vm-1",
                related_node_ids=(),
                metric="node.cpu_usage",
                value=float("nan"),
                dimensions=(),
                direction="high",
            )

    def test_evidence_rejects_non_finite_or_out_of_range_score(self):
        common = {
            "timestamp": datetime(2026, 7, 28, 12, 40, tzinfo=timezone.utc),
            "source": "node",
            "node_id": "xian-service-vm-1",
            "related_node_ids": (),
            "metric": "node.cpu_usage",
            "value": 99.0,
            "baseline": 1.0,
            "direction": "high",
            "dimensions": (),
            "summary": None,
        }
        for invalid_score in (float("inf"), -0.1, 25.1):
            with self.subTest(score=invalid_score), self.assertRaises(ValueError):
                AnomalyEvidence(score=invalid_score, **common)

        evidence = AnomalyEvidence(score=25.0, **common)
        self.assertTrue(math.isfinite(evidence.score))

    def test_evidence_rejects_invalid_semantic_score(self):
        common = {
            "timestamp": datetime(2026, 7, 28, 12, 40, tzinfo=timezone.utc),
            "source": "node",
            "node_id": "xian-service-vm-1",
            "related_node_ids": (),
            "metric": "node.cpu_usage",
            "value": 99.0,
            "baseline": 1.0,
            "score": 25.0,
            "direction": "high",
            "dimensions": (),
        }
        for invalid_score in (float("nan"), -0.1, 1.1):
            with self.subTest(semantic_score=invalid_score), self.assertRaises(ValueError):
                AnomalyEvidence(semantic_score=invalid_score, **common)

        self.assertEqual(AnomalyEvidence(semantic_score=0.75, **common).semantic_score, 0.75)

    def test_detected_event_requires_ordered_interval_and_evidence(self):
        point = AnomalyEvidence(
            timestamp=datetime(2026, 7, 28, 12, 40, tzinfo=timezone.utc),
            source="node",
            node_id="xian-service-vm-1",
            related_node_ids=(),
            metric="node.cpu_usage",
            value=99.0,
            baseline=1.0,
            score=12.0,
            direction="high",
            dimensions=(),
            summary=None,
        )
        with self.assertRaises(ValueError):
            DetectedEvent(
                start=point.timestamp,
                end=point.timestamp,
                peak_time=point.timestamp,
                confidence=0.8,
                evidence=(point,),
                source_counts={"node": 1},
            )

    def test_parse_stats_accumulates_source_counts(self):
        stats = ParseStats()
        stats.record_file("node")
        stats.record_row("node")
        stats.record_bad_row("node")

        self.assertEqual(stats.files_by_source, {"node": 1})
        self.assertEqual(stats.rows_by_source, {"node": 1})
        self.assertEqual(stats.bad_rows_by_source, {"node": 1})

    def test_parse_stats_conserves_valid_filtered_and_invalid_rows(self):
        stats = ParseStats()
        stats.record_row("node")
        stats.record_valid_row("node")
        stats.record_emitted("node", 3)
        stats.record_row("node")
        stats.record_filtered_row("node")
        stats.record_unknown_node("node")

        self.assertEqual(stats.valid_rows_by_source, {"node": 1})
        self.assertEqual(stats.filtered_rows_by_source, {"node": 1})
        self.assertEqual(stats.emitted_by_source, {"node": 3})
        self.assertEqual(stats.unknown_nodes_by_source, {"node": 1})
        self.assertTrue(stats.rows_conserved("node"))

    def test_parse_stats_tracks_source_time_range(self):
        stats = ParseStats()
        later = datetime(2026, 8, 19, 5, tzinfo=timezone.utc)
        earlier = datetime(2026, 8, 19, 4, tzinfo=timezone.utc)

        stats.record_timestamp("node", later)
        stats.record_timestamp("node", earlier)

        self.assertEqual(stats.first_timestamp_by_source["node"], earlier)
        self.assertEqual(stats.last_timestamp_by_source["node"], later)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from aiops_challenge_2026.config import load_public_config
from baseline.bian.classification.prototype_model import classify_event, validate_prototypes
from baseline.bian.localization.graph_fusion import RankingResult
from baseline.bian.preprocessing.observations import AnomalyEvidence, DetectedEvent


BASE = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
MODEL_CONFIG = json.loads(
    (Path(__file__).parents[1] / "baseline/bian/config/model_v1.json").read_text(encoding="utf-8")
)["classification"]
TAXONOMY = load_public_config("fault_taxonomy")


def point(
    metric: str,
    *,
    source: str = "node",
    direction: str = "high",
    node: str = "xian-service-vm-1",
    event_role: str = "trigger",
) -> AnomalyEvidence:
    return AnomalyEvidence(
        timestamp=BASE + timedelta(minutes=1),
        source=source,
        node_id=node,
        related_node_ids=(),
        metric=metric,
        value=80.0,
        baseline=1.0,
        score=20.0,
        direction=direction,
        dimensions=(),
        summary=None,
        event_role=event_role,
    )


def symptom(metric: str, *, related: tuple[str, ...]) -> AnomalyEvidence:
    return AnomalyEvidence(
        timestamp=BASE + timedelta(minutes=2),
        source="traffic",
        node_id="xian-traffic-vm",
        related_node_ids=related,
        metric=metric,
        value=10.0,
        baseline=1.0,
        score=25.0,
        direction="high",
        dimensions=(),
        summary=None,
    )


def event(*evidence: AnomalyEvidence) -> DetectedEvent:
    return DetectedEvent(
        start=BASE,
        end=BASE + timedelta(minutes=10),
        peak_time=BASE + timedelta(minutes=1),
        confidence=0.9,
        evidence=tuple(evidence),
        source_counts={item.source: 1 for item in evidence},
    )


def ranking(role: str = "service-vm-1") -> RankingResult:
    node = f"xian-{role}"
    candidate = {"node_id": node, "device_role": role, "rank": 1, "score": 0.9}
    return RankingResult(
        top5=[{"rank": 1, "network_element_id": node}],
        candidates=(candidate,),
        by_node={node: candidate},
    )


class PrototypeModelTests(unittest.TestCase):
    def test_neutral_traffic_activity_does_not_create_service_fault_signals(self):
        result = classify_event(
            event(point("traffic.dns.active", source="traffic")),
            ranking("traffic-vm"),
            TAXONOMY,
            MODEL_CONFIG,
        )

        self.assertNotIn("dns", result.signals)
        self.assertNotIn("service_error", result.signals)

    def test_configured_conflict_signals_penalize_an_incompatible_fault(self):
        taxonomy = {
            "fault_categories": [
                {"fault_name": "fault_a", "major_category": "a", "sub_category": "a1"},
                {"fault_name": "fault_b", "major_category": "b", "sub_category": "b1"},
            ]
        }
        config = {
            "role_prior_weight": 0.0,
            "conflict_weight": 1.0,
            "prototypes": {
                "fault_a": {"cpu": 1.0},
                "fault_b": {"cpu": 0.9, "role_service": 0.4},
            },
            "conflicts": {"fault_a": {"cpu": 1.0}},
        }

        result = classify_event(event(point("node.cpu_usage")), ranking(), taxonomy, config)

        self.assertEqual(result.category, {"major_category": "b", "sub_category": "b1"})

    def test_cpu_and_load_classify_as_resource_cpu_pressure(self):
        result = classify_event(
            event(point("node.cpu_usage"), point("node.load1")),
            ranking(),
            TAXONOMY,
            MODEL_CONFIG,
        )

        self.assertEqual(
            result.category,
            {"major_category": "resource", "sub_category": "cpu_pressure"},
        )

    def test_bgp_peer_down_classifies_as_bgp_session_down(self):
        result = classify_event(
            event(
                point(
                    "routing.bgp_peer_up",
                    source="routing",
                    direction="low",
                    node="xian-br-1",
                )
            ),
            ranking("br-1"),
            TAXONOMY,
            MODEL_CONFIG,
        )

        self.assertEqual(
            result.category,
            {"major_category": "routing", "sub_category": "bgp_session_down"},
        )

    def test_web_error_ratio_classifies_as_web_5xx(self):
        result = classify_event(
            event(point("traffic.web.error_ratio", source="traffic")),
            ranking("traffic-vm"),
            TAXONOMY,
            MODEL_CONFIG,
        )

        self.assertEqual(
            result.category,
            {"major_category": "service", "sub_category": "web_5xx"},
        )

    def test_direct_root_cpu_evidence_outweighs_downstream_service_symptoms(self):
        symptoms = tuple(
            symptom(metric, related=("xian-service-vm-1",))
            for _ in range(8)
            for metric in ("traffic.web.latency_p95_seconds", "traffic.web.error_ratio")
        )
        detected = event(point("node.cpu_usage"), point("node.load1"), *symptoms)

        result = classify_event(detected, ranking(), TAXONOMY, MODEL_CONFIG)

        self.assertEqual(
            result.category,
            {"major_category": "resource", "sub_category": "cpu_pressure"},
        )

    def test_specific_memory_drop_outweighs_reactive_disk_activity(self):
        detected = event(
            point("node.memory_available_ratio", direction="low"),
            point("node.disk_read_rate"),
            point("node.disk_write_rate"),
            point("node.disk_io_util"),
        )

        result = classify_event(detected, ranking(), TAXONOMY, MODEL_CONFIG)

        self.assertEqual(
            result.category,
            {"major_category": "resource", "sub_category": "memory_pressure"},
        )

    def test_all_taxonomy_entries_have_prototypes_and_result_is_a_legal_pair(self):
        self.assertEqual(validate_prototypes(TAXONOMY, MODEL_CONFIG), [])

        result = classify_event(event(point("node.process_count")), ranking(), TAXONOMY, MODEL_CONFIG)
        legal = {
            (item["major_category"], item["sub_category"])
            for item in TAXONOMY["fault_categories"]
        }
        self.assertIn(
            (result.category["major_category"], result.category["sub_category"]),
            legal,
        )
        self.assertLessEqual(len(result.top3), 3)
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)


if __name__ == "__main__":
    unittest.main()

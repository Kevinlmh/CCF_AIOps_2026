from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from baseline.bian.localization.graph_fusion import rank_candidates
from baseline.bian.preprocessing.observations import AnomalyEvidence, DetectedEvent


BASE = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
ROLES = (
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
NETWORK = {"cities": ["xian"], "device_roles": list(ROLES)}
TOPOLOGY = {
    "directed": False,
    "nodes": [{"node_id": f"xian-{role}"} for role in ROLES],
    "edges": [
        {"source": "xian-br-1", "target": "xian-cr-1", "relation": "routing_path"},
        {"source": "xian-br-2", "target": "xian-cr-2", "relation": "routing_path"},
        {"source": "xian-cr-1", "target": "xian-fw", "relation": "transit"},
        {"source": "xian-cr-2", "target": "xian-fw", "relation": "transit"},
        {"source": "xian-fw", "target": "xian-service-vm-1", "relation": "access"},
        {"source": "xian-fw", "target": "xian-service-vm-2", "relation": "access"},
        {"source": "xian-fw", "target": "xian-service-vm-3", "relation": "access"},
        {"source": "xian-fw", "target": "xian-traffic-vm", "relation": "access"},
    ],
}
CONFIG = {
    "weights": {
        "severity": 0.28,
        "persistence": 0.15,
        "precedence": 0.16,
        "source_diversity": 0.14,
        "directness": 0.16,
        "relational_support": 0.07,
        "topology_explanation": 0.08,
        "symptom_penalty": -0.04,
    }
}


def point(
    minute: int,
    node: str | None,
    metric: str,
    *,
    source: str,
    score: float = 15.0,
    related: tuple[str, ...] = (),
    event_role: str = "trigger",
    semantic_score: float = 0.0,
    sample_count: float | None = None,
    numerator_count: float | None = None,
) -> AnomalyEvidence:
    return AnomalyEvidence(
        timestamp=BASE + timedelta(minutes=minute),
        source=source,
        node_id=node,
        related_node_ids=related,
        metric=metric,
        value=80.0,
        baseline=1.0,
        score=score,
        direction="high",
        dimensions=(),
        summary=None,
        event_role=event_role,
        semantic_score=semantic_score,
        sample_count=sample_count,
        numerator_count=numerator_count,
    )


def event(*points: AnomalyEvidence) -> DetectedEvent:
    return DetectedEvent(
        start=BASE,
        end=BASE + timedelta(minutes=10),
        peak_time=BASE + timedelta(minutes=2),
        confidence=0.9,
        evidence=tuple(points),
        source_counts={source: sum(item.source == source for item in points) for source in {item.source for item in points}},
    )


class GraphFusionTests(unittest.TestCase):
    def test_serialized_ratio_evidence_exposes_sample_reliability(self):
        detected = event(
            point(
                1,
                "xian-traffic-vm",
                "traffic.web.error_ratio",
                source="traffic",
                score=20.0,
                related=(
                    "xian-service-vm-1",
                    "xian-service-vm-2",
                    "xian-service-vm-3",
                ),
                sample_count=60.0,
                numerator_count=18.0,
            )
        )

        result = rank_candidates(detected, NETWORK, TOPOLOGY, CONFIG)
        serialized = result.by_node["xian-traffic-vm"]["evidence"][0]

        self.assertEqual(serialized["sample_count"], 60.0)
        self.assertEqual(serialized["numerator_count"], 18.0)
        self.assertEqual(serialized["relation_type"], "traffic_observer")

    def test_traffic_target_candidates_are_not_generic_symptom_tail(self):
        detected = event(
            point(
                1,
                "xian-traffic-vm",
                "traffic.web.error_ratio",
                source="traffic",
                score=20.0,
                related=(
                    "xian-service-vm-1",
                    "xian-service-vm-2",
                    "xian-service-vm-3",
                ),
                sample_count=60.0,
                numerator_count=18.0,
            )
        )

        result = rank_candidates(detected, NETWORK, TOPOLOGY, CONFIG)
        target = result.by_node["xian-service-vm-1"]

        self.assertGreater(target["target_support"], 0.0)
        self.assertEqual(target["relation_type"], "traffic_target")
        self.assertLess(target["symptom_penalty"], 0.8)
        self.assertEqual(target["evidence"][0]["relation_type"], "traffic_target")

    def test_high_score_support_does_not_outrank_direct_trigger(self):
        detected = event(
            point(
                1,
                "xian-service-vm-1",
                "node.cpu_usage",
                source="node",
                score=15.0,
            ),
            point(
                1,
                "xian-monitor-vm",
                "node.disk_read_rate",
                source="node",
                score=25.0,
                event_role="support",
            ),
            point(
                2,
                "xian-monitor-vm",
                "node.disk_write_rate",
                source="node",
                score=25.0,
                event_role="support",
            ),
            point(
                3,
                "xian-monitor-vm",
                "netflow.bytes",
                source="netflow",
                score=25.0,
                event_role="support",
            ),
        )

        result = rank_candidates(
            detected,
            NETWORK,
            TOPOLOGY,
            {**CONFIG, "support_evidence_weight": 0.25},
        )

        self.assertEqual(
            result.top5[0]["network_element_id"], "xian-service-vm-1"
        )
        self.assertEqual(
            result.by_node["xian-monitor-vm"]["support_anomaly_count"], 3
        )
        self.assertEqual(
            result.by_node["xian-monitor-vm"]["trigger_anomaly_count"], 0
        )

    def test_explicit_cross_city_topology_edge_expands_candidate_scope(self):
        network = {
            "cities": ["xian", "beida", "wuhan"],
            "device_roles": list(ROLES),
        }
        topology = {
            "directed": False,
            "nodes": [
                {"node_id": f"{city}-{role}"}
                for city in network["cities"]
                for role in ROLES
            ],
            "edges": [
                {
                    "source": "xian-cr-1",
                    "target": "beida-cr-1",
                    "relation": "wan_peer",
                }
            ],
        }
        detected = event(
            point(1, "xian-cr-1", "routing.bgp_peer_up", source="routing")
        )

        result = rank_candidates(detected, network, topology, CONFIG)

        self.assertEqual(result.scope["direct_cities"], ["xian"])
        self.assertEqual(result.scope["topology_expanded_cities"], ["beida"])
        self.assertEqual(result.scope["candidate_cities"], ["beida", "xian"])
        self.assertEqual(result.scope["excluded_candidate_count"], len(ROLES))
    def test_unrelated_cities_cannot_fill_zero_score_top5_slots(self):
        network = {"cities": ["xian", "beida"], "device_roles": list(ROLES)}
        topology = {
            "directed": False,
            "nodes": [
                {"node_id": f"{city}-{role}"}
                for city in network["cities"]
                for role in ROLES
            ],
            "edges": [],
        }
        detected = event(
            point(1, "xian-cr-1", "routing.ipv6_route_count", source="routing")
        )

        result = rank_candidates(detected, network, topology, CONFIG)

        self.assertTrue(
            all(item["network_element_id"].startswith("xian-") for item in result.top5)
        )

    def test_direct_cpu_evidence_ranks_measured_service_first(self):
        detected = event(
            point(1, "xian-service-vm-1", "node.cpu_usage", source="node", score=20.0),
            point(2, "xian-service-vm-1", "node.load1", source="node", score=16.0),
            point(2, "xian-traffic-vm", "traffic.web.latency_p95_seconds", source="traffic", score=18.0,
                  related=("xian-service-vm-1", "xian-service-vm-2", "xian-service-vm-3")),
        )

        result = rank_candidates(detected, NETWORK, TOPOLOGY, CONFIG)

        self.assertEqual(result.top5[0], {"rank": 1, "network_element_id": "xian-service-vm-1"})
        self.assertGreater(
            result.by_node["xian-service-vm-1"]["directness"],
            result.by_node["xian-traffic-vm"]["directness"],
        )
        direct = result.by_node["xian-service-vm-1"]
        self.assertEqual(direct["relation_type"], "direct")
        self.assertTrue(
            all(item["relation_type"] == "direct" for item in direct["evidence"])
        )

    def test_temporal_precedence_breaks_equal_severity_routing_tie(self):
        detected = event(
            point(1, "xian-br-1", "routing.bgp_peer_up", source="routing", score=15.0),
            point(5, "xian-br-2", "routing.bgp_peer_up", source="routing", score=15.0),
        )

        result = rank_candidates(detected, NETWORK, TOPOLOGY, CONFIG)

        ordered = [item["network_element_id"] for item in result.top5]
        self.assertLess(ordered.index("xian-br-1"), ordered.index("xian-br-2"))
        self.assertGreater(
            result.by_node["xian-br-1"]["precedence"],
            result.by_node["xian-br-2"]["precedence"],
        )

    def test_precedence_applies_support_weight_to_each_point_before_aggregation(self):
        detected = event(
            point(
                0,
                "xian-br-1",
                "routing.route_count",
                source="routing",
                event_role="support",
            ),
            point(9, "xian-br-1", "routing.bgp_peer_up", source="routing"),
            point(5, "xian-br-2", "routing.bgp_peer_up", source="routing"),
        )

        result = rank_candidates(
            detected,
            NETWORK,
            TOPOLOGY,
            {**CONFIG, "support_evidence_weight": 0.25},
        )

        self.assertGreater(
            result.by_node["xian-br-2"]["precedence"],
            result.by_node["xian-br-1"]["precedence"],
        )

    def test_event_without_candidate_evidence_does_not_emit_zero_score_top5(self):
        detected = event(
            point(1, None, "frr.bgp_event", source="frr")
        )

        result = rank_candidates(detected, NETWORK, TOPOLOGY, CONFIG)

        self.assertEqual(result.top5, [])
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.scope["candidate_count"], 0)

    def test_relational_symptoms_support_but_do_not_override_direct_evidence(self):
        detected = event(
            point(1, "xian-fw", "interface.rx_drop_rate", source="interface", score=20.0),
            point(
                2,
                "xian-traffic-vm",
                "traffic.web.error_ratio",
                source="traffic",
                score=20.0,
                related=("xian-service-vm-1", "xian-service-vm-2", "xian-service-vm-3"),
            ),
        )

        result = rank_candidates(detected, NETWORK, TOPOLOGY, CONFIG)

        self.assertEqual(result.top5[0]["network_element_id"], "xian-fw")
        self.assertGreater(result.by_node["xian-service-vm-1"]["relational_support"], 0.0)
        self.assertGreater(result.by_node["xian-service-vm-1"]["symptom_penalty"], 0.0)

    def test_top5_is_deterministic_valid_unique_and_contiguously_ranked(self):
        detected = event(point(1, "xian-cr-1", "routing.ipv6_route_count", source="routing"))

        first = rank_candidates(detected, NETWORK, TOPOLOGY, CONFIG)
        second = rank_candidates(detected, NETWORK, TOPOLOGY, CONFIG)

        self.assertEqual(first.top5, second.top5)
        self.assertEqual([item["rank"] for item in first.top5], [1, 2, 3, 4, 5])
        ids = [item["network_element_id"] for item in first.top5]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(set(ids) <= {f"xian-{role}" for role in ROLES})

    def test_isolated_monitor_disk_noise_does_not_beat_service_with_correlated_symptoms(self):
        detected = event(
            point(1, "xian-service-vm-1", "node.cpu_usage", source="node", score=25.0),
            point(2, "xian-service-vm-1", "node.cpu_usage", source="node", score=25.0),
            point(3, "xian-service-vm-1", "node.load1", source="node", score=18.0),
            point(0, "xian-monitor-vm", "node.disk_read_rate", source="node", score=25.0),
            point(2, "xian-monitor-vm", "node.disk_read_rate", source="node", score=20.0),
            point(
                2,
                "xian-traffic-vm",
                "traffic.web.latency_p95_seconds",
                source="traffic",
                score=20.0,
                related=("xian-service-vm-1", "xian-service-vm-2", "xian-service-vm-3"),
            ),
        )

        result = rank_candidates(detected, NETWORK, TOPOLOGY, CONFIG)

        self.assertLess(
            result.by_node["xian-service-vm-1"]["rank"],
            result.by_node["xian-monitor-vm"]["rank"],
        )
        self.assertEqual(result.by_node["xian-monitor-vm"]["topology_explanation"], 0.0)


if __name__ == "__main__":
    unittest.main()

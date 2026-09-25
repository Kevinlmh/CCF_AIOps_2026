from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import torch

from aiops_challenge_2026.config import load_public_config
from aiops_v2.classification.semantic import classify_event, extract_candidate_signals
from aiops_v2.classification.semantic import SEMANTIC_SIGNAL_NAMES, _signal_name
from aiops_v2.data.feature_store import build_feature_store
from aiops_v2.events.decoder import DecodedEvent
from aiops_v2.localization.ranking import rank_root_causes
from aiops_v2.training.inference import TimelineScores
from baseline.bian.preprocessing.observations import NumericObservation


START = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)


def _numeric(
    minute: int,
    *,
    source: str,
    node: str,
    metric: str,
    value: float,
    direction: str,
    dimensions=(),
) -> NumericObservation:
    return NumericObservation(
        timestamp=START + timedelta(minutes=minute),
        source=source,
        node_id=node,
        related_node_ids=(),
        metric=metric,
        value=value,
        dimensions=dimensions,
        direction=direction,
    )


def _event() -> DecodedEvent:
    return DecodedEvent(2, 3, 2, 0.9, 2.0, START + timedelta(minutes=2), START + timedelta(minutes=4))


def test_classification_distinguishes_resource_cpu_from_firewall_cpu(tmp_path) -> None:
    observations = [
        _numeric(0, source="node", node="beida-service-vm-1", metric="node.cpu_usage", value=1, direction="high"),
        _numeric(1, source="node", node="beida-service-vm-1", metric="node.cpu_usage", value=2, direction="high"),
        _numeric(2, source="node", node="beida-service-vm-1", metric="node.cpu_usage", value=80, direction="high"),
        _numeric(3, source="node", node="beida-service-vm-1", metric="node.cpu_usage", value=90, direction="high"),
    ]
    store = build_feature_store(observations, load_public_config("network_elements"), tmp_path / "store")
    taxonomy = load_public_config("fault_taxonomy")

    service = classify_event(_event(), "beida-service-vm-1", store, taxonomy)
    firewall = classify_event(_event(), "beida-fw", store, taxonomy)

    assert service.category == {"major_category": "resource", "sub_category": "cpu_pressure"}
    assert firewall.category == {"major_category": "firewall", "sub_category": "cpu_pressure"}


def test_fixed_semantic_schema_covers_every_metric_mapping() -> None:
    representative_features = (
        "wrong_record",
        "rule_order",
        "port_block",
        "acl_drop",
        "rate_limit",
        "default_route",
        "static_route",
        "blackhole",
        "bgp_flap",
        "ospf_cost",
        "unique_dst_port",
        "cpu_usage",
        "memory_available_percent",
        "disk_io_util",
        "filesystem_usage",
        "process_count",
        "softirq",
        "bgp_state",
        "ospf_state",
        "drop_rate",
        "traffic.web.error_rate",
        "traffic.web.latency",
        "traffic.auth.timeout",
        "traffic.auth.error_rate",
        "traffic.auth.latency",
        "traffic.dns.success_rate",
        "traffic.dns.wrong_record",
        "node.latency",
        "node.throughput",
    )
    mapped = {
        signal
        for feature in representative_features
        if (mapping := _signal_name(feature)) is not None
        for signal, _ in (mapping,)
    }

    assert mapped <= set(SEMANTIC_SIGNAL_NAMES)


def test_throughput_decline_is_not_itself_a_rate_limit_diagnosis() -> None:
    assert _signal_name("traffic.elephant.throughput_bps") is None
    assert _signal_name("interface.rx_bytes_rate") is None
    assert _signal_name("interface.rx_packets_rate") is None
    assert _signal_name("interface.policer_drop_rate") == ("rate_limit", False)


def test_classification_recognizes_target_web_errors(tmp_path) -> None:
    dimensions = (("flow_type", "web"), ("target_region", "wuhan"))
    observations = [
        _numeric(0, source="traffic", node="chengdu-traffic-vm", metric="traffic.web.error_ratio", value=0.0, direction="high", dimensions=dimensions),
        _numeric(1, source="traffic", node="chengdu-traffic-vm", metric="traffic.web.error_ratio", value=0.0, direction="high", dimensions=dimensions),
        _numeric(2, source="traffic", node="chengdu-traffic-vm", metric="traffic.web.error_ratio", value=0.6, direction="high", dimensions=dimensions),
        _numeric(3, source="traffic", node="chengdu-traffic-vm", metric="traffic.web.error_ratio", value=0.7, direction="high", dimensions=dimensions),
    ]
    store = build_feature_store(observations, load_public_config("network_elements"), tmp_path / "store")

    result = classify_event(
        _event(),
        "wuhan-service-vm-1",
        store,
        load_public_config("fault_taxonomy"),
    )

    assert result.category == {"major_category": "service", "sub_category": "web_5xx"}
    assert result.signals["web_error"] > 0


def test_candidate_signal_extraction_slices_time_before_edge_gather(tmp_path) -> None:
    dimensions = (("flow_type", "web"), ("target_region", "wuhan"))
    observations = [
        _numeric(minute, source="traffic", node="chengdu-traffic-vm",
                 metric="traffic.web.error_rate", value=float(minute >= 2),
                 direction="high", dimensions=dimensions)
        for minute in range(5)
    ]
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )

    class GuardFullTime:
        def __init__(self, values):
            self.values = values

        def __getitem__(self, key):
            first = key[0]
            if isinstance(first, slice) and first.start is None and first.stop is None:
                raise AssertionError("entire timeline copied before edge selection")
            return self.values[key]

    store.edge_values = GuardFullTime(store.edge_values)
    store.edge_mask = GuardFullTime(store.edge_mask)
    result = extract_candidate_signals(_event(), "wuhan-service-vm-1", store)
    assert result["web_error"] > 0


def test_low_absolute_disk_utilization_does_not_overwhelm_cpu_evidence(tmp_path) -> None:
    node = "beida-service-vm-1"
    observations = []
    for minute in range(6):
        fault = minute >= 3
        observations.extend((
            _numeric(minute, source="node", node=node, metric="node.disk_io_util",
                     value=8 if fault else 1, direction="high"),
            _numeric(minute, source="node", node=node, metric="node.cpu_usage",
                     value=45 if fault else 2, direction="high"),
            _numeric(minute, source="node", node=node, metric="node.disk_read_rate",
                     value=100000 if fault else 0, direction="high"),
        ))
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    event = DecodedEvent(3, 5, 4, 0.95, 2.0, START + timedelta(minutes=3), START + timedelta(minutes=6))

    signals = extract_candidate_signals(event, node, store)
    classification = classify_event(event, node, store, load_public_config("fault_taxonomy"))

    assert signals["disk_io"] <= 2.0
    assert signals["cpu"] > signals["disk_io"]
    assert classification.category == {"major_category": "resource", "sub_category": "cpu_pressure"}


def test_high_absolute_disk_utilization_retains_strong_signal(tmp_path) -> None:
    node = "wuhan-service-vm-2"
    observations = [
        _numeric(minute, source="node", node=node, metric="node.disk_io_util",
                 value=95 if minute >= 3 else 2, direction="high")
        for minute in range(6)
    ]
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    event = DecodedEvent(3, 5, 4, 0.95, 2.0, START + timedelta(minutes=3), START + timedelta(minutes=6))

    signals = extract_candidate_signals(event, node, store)

    assert signals["disk_io"] >= 20.0


def test_disk_read_rate_alone_does_not_claim_disk_pressure_with_full_confidence(tmp_path) -> None:
    node = "beida-service-vm-1"
    observations = [
        _numeric(minute, source="node", node=node, metric="node.disk_read_rate",
                 value=100000 if minute >= 3 else 0, direction="high")
        for minute in range(6)
    ]
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    event = DecodedEvent(3, 5, 4, 0.95, 2.0, START + timedelta(minutes=3), START + timedelta(minutes=6))

    signals = extract_candidate_signals(event, node, store)
    classification = classify_event(event, node, store, load_public_config("fault_taxonomy"))

    assert signals["disk_io"] == 0.0
    assert classification.category != {"major_category": "resource", "sub_category": "disk_io_pressure"}
    assert classification.confidence == 0.0


def test_weak_root_disk_signal_does_not_receive_full_category_vote(tmp_path) -> None:
    root = "beida-service-vm-1"
    cpu_node = "wuhan-service-vm-2"
    observations = []
    for minute in range(6):
        fault = minute >= 3
        observations.extend((
            _numeric(minute, source="node", node=root, metric="node.disk_io_util",
                     value=4 if fault else 1, direction="high"),
            _numeric(minute, source="node", node=cpu_node, metric="node.cpu_usage",
                     value=45 if fault else 2, direction="high"),
        ))
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    event = DecodedEvent(3, 5, 4, 0.95, 2.0, START + timedelta(minutes=3), START + timedelta(minutes=6))
    timeline = TimelineScores(
        family=np.zeros((6, 3), dtype=np.float32),
        node=np.zeros((6, 80), dtype=np.float32),
        edge=np.zeros((6, len(store.entities.edges)), dtype=np.float32),
        log=np.zeros((6, 80), dtype=np.float32),
        coverage=np.ones(6, dtype=np.int32),
    )
    ranking = rank_root_causes(event, timeline, store)
    ranking = replace(
        ranking,
        scores={node: 99.0 if node == root else 1.0 if node == cpu_node else 0.0
                for node in store.entities.nodes},
    )

    result = classify_event(
        event, root, store, load_public_config("fault_taxonomy"), ranking=ranking
    )

    assert result.category == {"major_category": "resource", "sub_category": "cpu_pressure"}


def test_classification_output_always_belongs_to_official_taxonomy(tmp_path) -> None:
    store = build_feature_store(
        [_numeric(0, source="node", node="beida-br-1", metric="node.cpu_usage", value=1, direction="high")],
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    event = DecodedEvent(0, 0, 0, 0.9, 1.0, START, START + timedelta(minutes=1))
    taxonomy = load_public_config("fault_taxonomy")

    result = classify_event(event, "beida-br-1", store, taxonomy)

    legal = {(item["major_category"], item["sub_category"]) for item in taxonomy["fault_categories"]}
    assert (result.category["major_category"], result.category["sub_category"]) in legal


def test_wrong_city_root_top1_does_not_hide_memory_category_evidence(tmp_path) -> None:
    observations = [
        _numeric(0, source="node", node="guangzhou-service-vm-2", metric="node.memory_available_percent", value=90, direction="low"),
        _numeric(1, source="node", node="guangzhou-service-vm-2", metric="node.memory_available_percent", value=88, direction="low"),
        _numeric(2, source="node", node="guangzhou-service-vm-2", metric="node.memory_available_percent", value=10, direction="low"),
        _numeric(3, source="node", node="guangzhou-service-vm-2", metric="node.memory_available_percent", value=8, direction="low"),
    ]
    store = build_feature_store(
        observations,
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    node_scores = np.zeros((4, 80), dtype=np.float32)
    node_scores[2:4, store.entities.node_index("nanjing-service-vm-1")] = 10.0
    timeline = TimelineScores(
        family=np.ones((4, 3), dtype=np.float32),
        node=node_scores,
        edge=np.zeros((4, len(store.entities.edges)), dtype=np.float32),
        log=np.zeros((4, 80), dtype=np.float32),
        coverage=np.ones(4, dtype=np.int32),
    )

    ranking = rank_root_causes(_event(), timeline, store)
    result = classify_event(
        _event(),
        ranking.top5[0],
        store,
        load_public_config("fault_taxonomy"),
        ranking=ranking,
    )

    assert ranking.top5[0] == "nanjing-service-vm-1"
    assert result.category == {
        "major_category": "resource",
        "sub_category": "memory_pressure",
    }


def test_synthetic_head_cannot_override_strong_semantic_category_evidence(tmp_path) -> None:
    observations = [
        _numeric(0, source="node", node="guangzhou-service-vm-2", metric="node.memory_available_percent", value=90, direction="low"),
        _numeric(1, source="node", node="guangzhou-service-vm-2", metric="node.memory_available_percent", value=88, direction="low"),
        _numeric(2, source="node", node="guangzhou-service-vm-2", metric="node.memory_available_percent", value=10, direction="low"),
        _numeric(3, source="node", node="guangzhou-service-vm-2", metric="node.memory_available_percent", value=8, direction="low"),
    ]
    store = build_feature_store(
        observations,
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    node_scores = np.zeros((4, 80), dtype=np.float32)
    node_scores[2:4, store.entities.node_index("nanjing-service-vm-1")] = 10.0
    timeline = TimelineScores(
        family=np.ones((4, 3), dtype=np.float32),
        node=node_scores,
        edge=np.zeros((4, len(store.entities.edges)), dtype=np.float32),
        log=np.zeros((4, 80), dtype=np.float32),
        coverage=np.ones(4, dtype=np.int32),
    )
    taxonomy = load_public_config("fault_taxonomy")
    cpu_index = next(
        index
        for index, item in enumerate(taxonomy["fault_categories"])
        if item["fault_name"] == "resource_cpu_high"
    )

    class CpuOnlyHead:
        category_count = len(taxonomy["fault_categories"])

        def to(self, _device):
            return self

        def eval(self):
            return self

        def __call__(self, candidates, event_features, candidate_mask):
            batch, count, _ = candidates.shape
            roots = torch.zeros((batch, count), device=candidates.device)
            categories = torch.zeros(
                (batch, count, self.category_count), device=candidates.device
            )
            categories[:, :, cpu_index] = 8.0
            return roots, categories

    heads = CpuOnlyHead()
    ranking = rank_root_causes(_event(), timeline, store, diagnosis_heads=heads)
    result = classify_event(
        _event(),
        ranking.top5[0],
        store,
        taxonomy,
        ranking=ranking,
        diagnosis_heads=heads,
    )

    assert result.category == {
        "major_category": "resource",
        "sub_category": "memory_pressure",
    }

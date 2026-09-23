from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiops_challenge_2026.config import load_public_config
from aiops_v2.classification.semantic import classify_event
from aiops_v2.data.feature_store import build_feature_store
from aiops_v2.events.decoder import DecodedEvent
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

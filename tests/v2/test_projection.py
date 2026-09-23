from __future__ import annotations

from datetime import datetime, timezone

from aiops_v2.contracts import EdgeKey
from aiops_v2.data.projection import project_observation
from baseline.bian.preprocessing.observations import NumericObservation


NOW = datetime(2026, 8, 19, 4, 2, 31, tzinfo=timezone.utc)


def _observation(
    *,
    source: str,
    metric: str,
    value: float,
    node_id: str = "chengdu-traffic-vm",
    direction: str = "both",
    dimensions: tuple[tuple[str, str], ...] = (),
) -> NumericObservation:
    return NumericObservation(
        timestamp=NOW,
        source=source,
        node_id=node_id,
        related_node_ids=(),
        metric=metric,
        value=value,
        dimensions=dimensions,
        direction=direction,
    )


def test_traffic_projection_uses_target_service_edge_not_observer_node() -> None:
    observation = _observation(
        source="traffic",
        metric="traffic.web.error_ratio",
        value=0.25,
        direction="high",
        dimensions=(
            ("flow_type", "web"),
            ("target_region", "wuhan"),
            ("target_domain", "web.wuhan.aiops.local"),
        ),
    )

    projected = project_observation(observation)

    assert len(projected) == 1
    assert projected[0].minute == datetime(2026, 8, 19, 4, 2, tzinfo=timezone.utc)
    assert projected[0].modality == "edge"
    assert projected[0].entity == EdgeKey(
        "chengdu-traffic-vm", "service-group:wuhan:web", "traffic"
    )
    assert projected[0].feature == "traffic.web.error_ratio"
    assert projected[0].aggregation == "max"


def test_success_ratio_uses_minimum_across_series() -> None:
    observation = _observation(
        source="traffic",
        metric="traffic.auth.success_ratio",
        value=0.8,
        direction="low",
        dimensions=(("flow_type", "auth"), ("target_region", "beida")),
    )

    projected = project_observation(observation)

    assert projected[0].aggregation == "min"


def test_netflow_projection_keeps_interface_and_protocol_on_an_edge() -> None:
    observation = _observation(
        source="netflow",
        node_id="chengdu-cr-1",
        metric="netflow.bytes",
        value=4096.0,
        dimensions=(("interface_id", "ens5"), ("protocol", "6")),
    )

    projected = project_observation(observation)

    assert projected[0].modality == "edge"
    assert projected[0].entity == EdgeKey(
        "chengdu-cr-1", "interface:chengdu-cr-1:ens5", "netflow"
    )
    assert projected[0].feature == "netflow.bytes.protocol_6"
    assert projected[0].aggregation == "sum"


def test_frr_projection_creates_log_feature_from_dimensions() -> None:
    observation = _observation(
        source="frr",
        node_id="chengdu-br-1",
        metric="frr.event_count",
        value=2.0,
        direction="high",
        dimensions=(("event_family", "bgp"), ("severity", "warning")),
    )

    projected = project_observation(observation)

    assert projected[0].modality == "log"
    assert projected[0].entity == "chengdu-br-1"
    assert projected[0].feature == "frr.bgp.warning.count"
    assert projected[0].aggregation == "sum"


def test_unknown_node_observation_is_not_projected() -> None:
    observation = _observation(
        source="node",
        node_id=None,
        metric="node.cpu_usage",
        value=90.0,
    )

    assert project_observation(observation) == ()

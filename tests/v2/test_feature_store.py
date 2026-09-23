from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from aiops_challenge_2026.config import load_public_config
from aiops_v2.contracts import EdgeKey
from aiops_v2.data.feature_store import FeatureStore, build_feature_store
from baseline.bian.preprocessing.observations import NumericObservation


START = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)


def _numeric(
    minute: int,
    *,
    source: str,
    node: str,
    metric: str,
    value: float,
    direction: str = "high",
    dimensions: tuple[tuple[str, str], ...] = (),
) -> NumericObservation:
    return NumericObservation(
        timestamp=START + timedelta(minutes=minute, seconds=10),
        source=source,
        node_id=node,
        related_node_ids=(),
        metric=metric,
        value=value,
        dimensions=dimensions,
        direction=direction,
    )


def test_feature_store_aggregates_values_and_preserves_missing_mask(tmp_path) -> None:
    observations = [
        _numeric(
            0,
            source="node",
            node="chengdu-br-1",
            metric="node.cpu_usage",
            value=10.0,
        ),
        _numeric(
            0,
            source="node",
            node="chengdu-br-1",
            metric="node.cpu_usage",
            value=25.0,
        ),
        _numeric(
            2,
            source="node",
            node="chengdu-br-1",
            metric="node.cpu_usage",
            value=15.0,
        ),
    ]

    store = build_feature_store(
        observations,
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    node = store.entities.node_index("chengdu-br-1")
    feature = store.features.index("node", "node.cpu_usage")
    assert store.node_values.shape == (3, 80, 1)
    assert float(store.node_values[0, node, feature]) == 25.0
    assert bool(store.node_mask[0, node, feature]) is True
    assert bool(store.node_mask[1, node, feature]) is False
    assert float(store.node_values[1, node, feature]) == 0.0
    assert store.start_time == START
    assert store.end_time == START + timedelta(minutes=2)


def test_feature_store_models_traffic_as_source_to_target_edges(tmp_path) -> None:
    common = (
        ("flow_type", "web"),
        ("target_region", "wuhan"),
        ("target_domain", "web.wuhan.aiops.local"),
    )
    observations = [
        _numeric(
            0,
            source="traffic",
            node="chengdu-traffic-vm",
            metric="traffic.web.error_ratio",
            value=0.2,
            dimensions=common,
        ),
        _numeric(
            0,
            source="traffic",
            node="xian-traffic-vm",
            metric="traffic.web.error_ratio",
            value=0.3,
            dimensions=common,
        ),
    ]

    store = build_feature_store(
        observations,
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    first = store.entities.edge_index(
        EdgeKey("chengdu-traffic-vm", "service-group:wuhan:web", "traffic")
    )
    second = store.entities.edge_index(
        EdgeKey("xian-traffic-vm", "service-group:wuhan:web", "traffic")
    )
    feature = store.features.index("edge", "traffic.web.error_ratio")
    assert store.edge_values.shape == (1, 2, 1)
    assert float(store.edge_values[0, first, feature]) == pytest.approx(0.2)
    assert float(store.edge_values[0, second, feature]) == pytest.approx(0.3)


def test_feature_store_round_trip_uses_memory_mapped_arrays(tmp_path) -> None:
    path = tmp_path / "store"
    build_feature_store(
        [
            _numeric(
                0,
                source="frr",
                node="beida-br-1",
                metric="frr.event_count",
                value=2.0,
                dimensions=(("event_family", "bgp"), ("severity", "warning")),
            )
        ],
        load_public_config("network_elements"),
        path,
    )

    restored = FeatureStore.open(path)

    assert isinstance(restored.log_values, np.memmap)
    assert restored.features.names("log") == ("frr.bgp.warning.count",)
    node = restored.entities.node_index("beida-br-1")
    assert float(restored.log_values[0, node, 0]) == 2.0
    assert restored.manifest["observation_count"] == 1


def test_feature_store_rejects_nonempty_destination(tmp_path) -> None:
    path = tmp_path / "store"
    path.mkdir()
    (path / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        build_feature_store([], load_public_config("network_elements"), path)


import pytest

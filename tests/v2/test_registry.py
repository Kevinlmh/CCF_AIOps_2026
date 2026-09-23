from __future__ import annotations

import pytest

from aiops_challenge_2026.config import load_public_config
from aiops_v2.contracts import EdgeKey
from aiops_v2.data.registry import EntityRegistry


def test_registry_builds_all_public_nodes_in_config_order() -> None:
    config = load_public_config("network_elements")

    registry = EntityRegistry.from_network_config(config)

    assert len(registry.nodes) == 80
    assert registry.nodes[:3] == (
        "beida-br-1",
        "beida-br-2",
        "beida-cr-1",
    )
    assert registry.nodes[-1] == "guangzhou-monitor-vm"
    assert registry.node_index("wuhan-fw") == 44


def test_registry_freezes_edges_in_deterministic_order() -> None:
    config = load_public_config("network_elements")
    registry = EntityRegistry.from_network_config(config)
    later = EdgeKey("wuhan-traffic-vm", "service-group:wuhan:web", "traffic")
    earlier = EdgeKey("beida-traffic-vm", "service-group:wuhan:web", "traffic")

    registry.add_edge(later)
    registry.add_edge(earlier)
    registry.add_edge(later)
    registry.freeze()

    assert registry.edges == (earlier, later)
    assert registry.edge_index(later) == 1
    with pytest.raises(RuntimeError, match="frozen"):
        registry.add_edge(EdgeKey("xian-br-1", "xian-br-2", "topology"))


def test_registry_round_trip_preserves_indices() -> None:
    config = load_public_config("network_elements")
    registry = EntityRegistry.from_network_config(config)
    registry.add_edge(EdgeKey("chengdu-br-1", "chengdu-br-2", "topology"))
    registry.freeze()

    restored = EntityRegistry.from_dict(registry.to_dict())

    assert restored.nodes == registry.nodes
    assert restored.edges == registry.edges
    assert restored.node_index("chengdu-br-2") == registry.node_index("chengdu-br-2")
    assert restored.edge_index(registry.edges[0]) == 0

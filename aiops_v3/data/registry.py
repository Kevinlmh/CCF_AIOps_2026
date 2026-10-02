"""Stable node and directed-edge indexing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aiops_v3.data.contracts import EdgeKey


class EntityRegistry:
    """Assign stable integer indices to official nodes and observed edges."""

    def __init__(self, nodes: Sequence[str]) -> None:
        clean = tuple(str(node) for node in nodes)
        if not clean or len(set(clean)) != len(clean):
            raise ValueError("nodes must be non-empty and unique")
        self._nodes = clean
        self._node_to_index = {node: index for index, node in enumerate(clean)}
        self._pending_edges: set[EdgeKey] = set()
        self._edges: tuple[EdgeKey, ...] = ()
        self._edge_to_index: dict[EdgeKey, int] = {}
        self._frozen = False

    @classmethod
    def from_network_config(cls, config: Mapping[str, Any]) -> "EntityRegistry":
        cities = tuple(config["cities"])
        roles = tuple(config["device_roles"])
        return cls(tuple(f"{city}-{role}" for city in cities for role in roles))

    @property
    def nodes(self) -> tuple[str, ...]:
        return self._nodes

    @property
    def edges(self) -> tuple[EdgeKey, ...]:
        if not self._frozen:
            raise RuntimeError("edge registry must be frozen before reading indices")
        return self._edges

    @property
    def frozen(self) -> bool:
        return self._frozen

    def node_index(self, node_id: str) -> int:
        try:
            return self._node_to_index[node_id]
        except KeyError as exc:
            raise KeyError(f"unknown public node: {node_id}") from exc

    def add_edge(self, edge: EdgeKey) -> None:
        if self._frozen:
            raise RuntimeError("entity registry is frozen")
        self._pending_edges.add(edge)

    def freeze(self) -> None:
        if self._frozen:
            return
        self._edges = tuple(sorted(self._pending_edges))
        self._edge_to_index = {edge: index for index, edge in enumerate(self._edges)}
        self._pending_edges.clear()
        self._frozen = True

    def edge_index(self, edge: EdgeKey) -> int:
        if not self._frozen:
            raise RuntimeError("edge registry must be frozen before reading indices")
        try:
            return self._edge_to_index[edge]
        except KeyError as exc:
            raise KeyError(f"unknown edge: {edge}") from exc

    def to_dict(self) -> dict[str, Any]:
        if not self._frozen:
            raise RuntimeError("entity registry must be frozen before serialization")
        return {
            "nodes": list(self._nodes),
            "edges": [edge.to_dict() for edge in self._edges],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EntityRegistry":
        result = cls(value["nodes"])
        for raw in value.get("edges", []):
            result.add_edge(EdgeKey.from_dict(raw))
        result.freeze()
        return result

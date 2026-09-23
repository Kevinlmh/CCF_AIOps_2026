"""Event-subgraph construction and target-aware root-cause ranking."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from aiops_v2.data.feature_store import FeatureStore
from aiops_v2.events.decoder import DecodedEvent
from aiops_v2.training.inference import TimelineScores


@dataclass(frozen=True, slots=True)
class RootRanking:
    top5: tuple[str, ...]
    scores: dict[str, float]
    explanations: dict[str, dict[str, float]]
    event_subgraph: dict[str, object]


def _temporal_strength(values: np.ndarray) -> np.ndarray:
    if values.shape[0] == 0:
        return np.zeros(values.shape[1:], dtype=np.float32)
    return 0.7 * np.max(values, axis=0) + 0.3 * np.mean(values, axis=0)


def _phase_slices(event: DecodedEvent, minute_count: int) -> dict[str, tuple[int, int]]:
    start = max(0, min(minute_count, event.start_index))
    end = max(start, min(minute_count, event.end_index + 1))
    duration = max(1, end - start)
    return {
        "before": (max(0, start - 10), start),
        "during": (start, end),
        "early": (start, min(end, start + max(1, duration // 3))),
        "recovery": (end, min(minute_count, end + 15)),
    }


def _strength(values: np.ndarray, bounds: tuple[int, int]) -> np.ndarray:
    start, end = bounds
    return _temporal_strength(values[start:end])


def rank_root_causes(
    event: DecodedEvent,
    timeline: TimelineScores,
    store: FeatureStore,
) -> RootRanking:
    """Build a bounded event graph and rank official nodes from temporal evidence.

    The score rewards evidence already present near event onset and evidence that
    subsides after recovery. Traffic is attributed primarily to its target service
    group; the source probe is retained only as weak observer evidence.
    """
    minute_count = min(
        timeline.node.shape[0], timeline.edge.shape[0], timeline.log.shape[0]
    )
    phases = _phase_slices(event, minute_count)
    if phases["during"][0] >= phases["during"][1]:
        raise ValueError("event does not overlap the score timeline")

    nodes = store.entities.nodes
    total = np.zeros(len(nodes), dtype=np.float64)
    components = {
        node: {
            "direct": 0.0,
            "direct_early": 0.0,
            "direct_pre": 0.0,
            "recovery": 0.0,
            "log": 0.0,
            "traffic_target": 0.0,
            "traffic_observer": 0.0,
            "netflow": 0.0,
        }
        for node in nodes
    }

    direct = _strength(timeline.node, phases["during"])
    early = _strength(timeline.node, phases["early"])
    before = _strength(timeline.node, phases["before"])
    after = _strength(timeline.node, phases["recovery"])
    logs = _strength(timeline.log, phases["during"])
    for index, node in enumerate(nodes):
        recovery = max(0.0, float(direct[index]) - float(after[index]))
        # Peak/duration are primary. Earlier direct evidence and recovery add
        # bounded support, rather than allowing a late symptom to dominate.
        score = (
            0.50 * float(direct[index])
            + 0.20 * float(early[index])
            + 0.15 * float(before[index])
            + 0.15 * recovery
            + 1.10 * float(logs[index])
        )
        components[node]["direct"] = float(direct[index])
        components[node]["direct_early"] = float(early[index])
        components[node]["direct_pre"] = float(before[index])
        components[node]["recovery"] = recovery
        components[node]["log"] = float(logs[index])
        total[index] += score

    edge_strength = _strength(timeline.edge, phases["during"])
    edge_early = _strength(timeline.edge, phases["early"])
    event_edges: list[dict[str, object]] = []
    for edge_index, edge in enumerate(store.entities.edges):
        strength = float(edge_strength[edge_index])
        if strength <= 0.0:
            continue
        early_strength = float(edge_early[edge_index])
        edge_record = {
            "source": edge.source,
            "target": edge.target,
            "relation": edge.relation,
            "during_score": strength,
            "early_score": early_strength,
        }
        event_edges.append(edge_record)
        if edge.relation == "traffic" and edge.target.startswith("service-group:"):
            _, city, _ = edge.target.split(":", 2)
            # The public files identify a target service group, not one VM.
            # Keep all three candidates but distribute evidence so repeated
            # observer-city rows do not multiply a single edge's mass.
            contribution = (strength + 0.25 * early_strength) / 3.0
            for role in ("service-vm-1", "service-vm-2", "service-vm-3"):
                node = f"{city}-{role}"
                if node in components:
                    components[node]["traffic_target"] += contribution
                    total[store.entities.node_index(node)] += contribution
            if edge.source in components:
                observer = 0.025 * strength
                components[edge.source]["traffic_observer"] += observer
                total[store.entities.node_index(edge.source)] += observer
        elif edge.relation == "netflow" and edge.source in components:
            support = 0.25 * strength + 0.10 * early_strength
            components[edge.source]["netflow"] += support
            total[store.entities.node_index(edge.source)] += support

    order = sorted(range(len(nodes)), key=lambda index: (-float(total[index]), index))
    top5 = tuple(nodes[index] for index in order[:5])
    subgraph_nodes = [
        {"node_id": node, "score": float(total[index]), "components": components[node]}
        for index, node in enumerate(nodes)
        if total[index] > 0
    ]
    subgraph_nodes.sort(key=lambda item: (-float(item["score"]), str(item["node_id"])))
    event_edges.sort(
        key=lambda item: (-float(item["during_score"]), str(item["source"]), str(item["target"]))
    )
    return RootRanking(
        top5=top5,
        scores={node: float(total[index]) for index, node in enumerate(nodes)},
        explanations=components,
        event_subgraph={
            "phases": {name: list(bounds) for name, bounds in phases.items()},
            "nodes": subgraph_nodes[:20],
            "edges": event_edges[:20],
            "truncated_node_count": max(0, len(subgraph_nodes) - 20),
            "truncated_edge_count": max(0, len(event_edges) - 20),
        },
    )

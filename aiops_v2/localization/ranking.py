"""Event-subgraph construction and candidate-conditioned root evidence."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch

from aiops_v2.classification.semantic import (
    extract_candidate_signals,
)
from aiops_v2.data.feature_store import FeatureStore
from aiops_v2.events.decoder import DecodedEvent
from aiops_v2.diagnosis_schema import EVENT_FEATURE_NAMES, ROOT_FEATURE_NAMES
from aiops_v2.training.inference import TimelineScores


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    candidate_nodes: tuple[str, ...]
    feature_names: tuple[str, ...]
    features: np.ndarray
    event_feature_names: tuple[str, ...]
    event_features: np.ndarray
    candidate_mask: np.ndarray
    observation_mask: np.ndarray
    event_edges: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        if self.features.shape != (len(self.candidate_nodes), len(self.feature_names)):
            raise ValueError("candidate evidence matrix shape does not match its schema")
        if self.event_features.shape != (len(self.event_feature_names),):
            raise ValueError("event evidence vector shape does not match its schema")
        if self.candidate_mask.shape != (len(self.candidate_nodes),):
            raise ValueError("candidate mask shape does not match candidate nodes")
        if self.observation_mask.shape != (len(self.candidate_nodes),):
            raise ValueError("observation mask shape does not match candidate nodes")
        if not np.isfinite(self.features).all() or not np.isfinite(self.event_features).all():
            raise ValueError("candidate evidence must be finite")


@dataclass(frozen=True, slots=True)
class RootRanking:
    top5: tuple[str, ...]
    scores: dict[str, float]
    explanations: dict[str, dict[str, float]]
    event_subgraph: dict[str, object]
    candidate_evidence: CandidateEvidence


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


def _node_role_features(node: str) -> dict[str, float]:
    return {
        "role_service": float("-service-vm-" in node),
        "role_firewall": float(node.endswith("-fw")),
        "role_router": float("-br-" in node or "-cr-" in node),
        "role_traffic_observer": float(node.endswith("-traffic-vm")),
    }


def extract_candidate_evidence(
    event: DecodedEvent,
    timeline: TimelineScores,
    store: FeatureStore,
) -> CandidateEvidence:
    """Extract event-local numeric features for every official root candidate."""
    minute_count = min(
        timeline.node.shape[0],
        timeline.edge.shape[0],
        timeline.log.shape[0],
        int(store.manifest["minute_count"]),
    )
    phases = _phase_slices(event, minute_count)
    start, end = phases["during"]
    if start >= end:
        raise ValueError("event does not overlap the score timeline")

    nodes = tuple(store.entities.nodes)
    node_indexes = {node: index for index, node in enumerate(nodes)}
    columns = {name: index for index, name in enumerate(ROOT_FEATURE_NAMES)}
    features = np.zeros((len(nodes), len(ROOT_FEATURE_NAMES)), dtype=np.float32)
    observation_mask = np.zeros(len(nodes), dtype=bool)

    direct = _strength(timeline.node, phases["during"])
    early = _strength(timeline.node, phases["early"])
    before = _strength(timeline.node, phases["before"])
    after = _strength(timeline.node, phases["recovery"])
    logs = _strength(timeline.log, phases["during"])
    during_node = np.asarray(timeline.node[start:end], dtype=np.float64)

    for index, node in enumerate(nodes):
        recovery = max(0.0, float(direct[index]) - float(after[index]))
        peak = float(np.max(during_node[:, index])) if during_node.size else 0.0
        mean = float(np.mean(during_node[:, index])) if during_node.size else 0.0
        values = {
            "direct": float(direct[index]),
            "direct_early": float(early[index]),
            "direct_pre": float(before[index]),
            "direct_persistence": mean / peak if peak > 0 else 0.0,
            "recovery": recovery,
            "log": float(logs[index]),
            **_node_role_features(node),
        }
        for name, value in values.items():
            features[index, columns[name]] = value
        observation_mask[index] = bool(
            store.node_mask[start:end, index].any()
            or store.log_mask[start:end, index].any()
        )

    traffic_indexes_by_city: dict[str, list[int]] = {}
    netflow_indexes_by_node: dict[str, list[int]] = {}
    event_edges: list[dict[str, object]] = []
    edge_strength = _strength(timeline.edge, phases["during"])
    edge_early = _strength(timeline.edge, phases["early"])
    for edge_index, edge in enumerate(store.entities.edges):
        if edge.relation == "traffic" and edge.target.startswith("service-group:"):
            _, city, _ = edge.target.split(":", 2)
            traffic_indexes_by_city.setdefault(city, []).append(edge_index)
            if edge.source in node_indexes:
                netflow_indexes_by_node.setdefault(edge.source, [])
            strength = float(edge_strength[edge_index])
            if strength > 0.0:
                event_edges.append(
                    {
                        "source": edge.source,
                        "target": edge.target,
                        "relation": edge.relation,
                        "during_score": strength,
                        "early_score": float(edge_early[edge_index]),
                    }
                )
            contribution = (strength + 0.25 * float(edge_early[edge_index])) / 3.0
            for role in ("service-vm-1", "service-vm-2", "service-vm-3"):
                candidate = f"{city}-{role}"
                if candidate in node_indexes:
                    row = node_indexes[candidate]
                    features[row, columns["traffic_target"]] += contribution
                    observation_mask[row] |= bool(store.edge_mask[start:end, edge_index].any())
            if edge.source in node_indexes:
                row = node_indexes[edge.source]
                features[row, columns["traffic_observer"]] += 0.025 * strength
                observation_mask[row] |= bool(store.edge_mask[start:end, edge_index].any())
        elif edge.relation == "netflow" and edge.source in node_indexes:
            netflow_indexes_by_node.setdefault(edge.source, []).append(edge_index)
            strength = float(edge_strength[edge_index])
            support = 0.25 * strength + 0.10 * float(edge_early[edge_index])
            row = node_indexes[edge.source]
            features[row, columns["netflow"]] += support
            observation_mask[row] |= bool(store.edge_mask[start:end, edge_index].any())
            if strength > 0.0:
                event_edges.append(
                    {
                        "source": edge.source,
                        "target": edge.target,
                        "relation": edge.relation,
                        "during_score": strength,
                        "early_score": float(edge_early[edge_index]),
                    }
                )

    for node_index, node in enumerate(nodes):
        city = node.split("-", 1)[0]
        selected_edges = tuple(
            traffic_indexes_by_city.get(city, ()) if "-service-vm-" in node else ()
        ) + tuple(netflow_indexes_by_node.get(node, ()))
        semantic = extract_candidate_signals(
            event,
            node,
            store,
            edge_indexes=selected_edges,
        )
        for signal, value in semantic.items():
            features[node_index, columns[f"signal_{signal}"]] = value

    for node_index in range(len(nodes)):
        features[node_index, columns["observed"]] = float(observation_mask[node_index])

    event_features = np.asarray(
        [
            float(event.duration_minutes),
            float(event.confidence),
            float(event.score),
            float(np.max(timeline.family[start:end, 0])) if end > start else 0.0,
            float(np.max(timeline.family[start:end, 1])) if end > start else 0.0,
            float(np.max(timeline.family[start:end, 2])) if end > start else 0.0,
        ],
        dtype=np.float32,
    )
    event_edges.sort(
        key=lambda item: (-float(item["during_score"]), str(item["source"]), str(item["target"]))
    )
    return CandidateEvidence(
        candidate_nodes=nodes,
        feature_names=ROOT_FEATURE_NAMES,
        features=features,
        event_feature_names=EVENT_FEATURE_NAMES,
        event_features=event_features,
        candidate_mask=np.ones(len(nodes), dtype=bool),
        observation_mask=observation_mask,
        event_edges=tuple(event_edges),
    )


def rank_root_causes(
    event: DecodedEvent,
    timeline: TimelineScores,
    store: FeatureStore,
    *,
    diagnosis_heads=None,
    device: str = "cpu",
) -> RootRanking:
    """Rank public candidates with direct evidence primary and observer support bounded."""
    evidence = extract_candidate_evidence(event, timeline, store)
    columns = {name: index for index, name in enumerate(evidence.feature_names)}
    nodes = evidence.candidate_nodes
    components = {
        node: {
            name: float(evidence.features[index, feature_index])
            for feature_index, name in enumerate(evidence.feature_names)
        }
        for index, node in enumerate(nodes)
    }
    total = np.zeros(len(nodes), dtype=np.float64)
    for index, node in enumerate(nodes):
        row = evidence.features[index]
        total[index] = (
            0.50 * row[columns["direct"]]
            + 0.20 * row[columns["direct_early"]]
            + 0.15 * row[columns["direct_pre"]]
            + 0.15 * row[columns["recovery"]]
            + 1.10 * row[columns["log"]]
            + 0.15 * row[columns["traffic_target"]]
            + row[columns["traffic_observer"]]
            + row[columns["netflow"]]
        )

    if diagnosis_heads is not None:
        candidate_features = torch.from_numpy(evidence.features).unsqueeze(0).to(device=device)
        event_features = torch.from_numpy(evidence.event_features).unsqueeze(0).to(device=device)
        candidate_mask = torch.from_numpy(evidence.candidate_mask).unsqueeze(0).to(device=device)
        diagnosis_heads = diagnosis_heads.to(device)
        diagnosis_heads.eval()
        with torch.no_grad():
            learned_root_logits, _ = diagnosis_heads(
                candidate_features,
                event_features,
                candidate_mask,
            )
            learned_probability = torch.softmax(learned_root_logits[0], dim=-1).cpu().numpy()
        rule_logits = np.log1p(np.maximum(total, 0.0))
        rule_logits -= np.max(rule_logits)
        rule_probability = np.exp(rule_logits)
        rule_probability /= rule_probability.sum()
        total = 0.75 * rule_probability + 0.25 * learned_probability
        for index, node in enumerate(nodes):
            components[node]["deterministic_root_probability"] = float(rule_probability[index])
            components[node]["learned_root_probability"] = float(learned_probability[index])
            components[node]["combined_root_probability"] = float(total[index])

    order = sorted(range(len(nodes)), key=lambda index: (-float(total[index]), index))
    top5 = tuple(nodes[index] for index in order[:5])
    evidence_columns = [
        column
        for column, name in enumerate(evidence.feature_names)
        if name not in {"role_service", "role_firewall", "role_router", "role_traffic_observer", "observed"}
    ]
    subgraph_nodes = [
        {"node_id": node, "score": float(total[index]), "components": components[node]}
        for index, node in enumerate(nodes)
        if evidence.observation_mask[index]
        and np.any(evidence.features[index, evidence_columns] > 0)
    ]
    subgraph_nodes.sort(key=lambda item: (-float(item["score"]), str(item["node_id"])))
    event_edges = sorted(
        evidence.event_edges,
        key=lambda item: (-float(item["during_score"]), str(item["source"]), str(item["target"])),
    )
    phases = _phase_slices(event, min(len(timeline.node), int(store.manifest["minute_count"])))
    return RootRanking(
        top5=top5,
        scores={node: float(total[index]) for index, node in enumerate(nodes)},
        explanations=components,
        event_subgraph={
            "phases": {name: list(bounds) for name, bounds in phases.items()},
            "nodes": subgraph_nodes[:20],
            "edges": list(event_edges[:20]),
            "truncated_node_count": max(0, len(subgraph_nodes) - 20),
            "truncated_edge_count": max(0, len(event_edges) - 20),
        },
        candidate_evidence=evidence,
    )

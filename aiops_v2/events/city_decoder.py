"""Decode independent city timelines for the direct detector.

The global maximum is useful for monitoring, but it must not collapse two
simultaneous incidents in different cities into one event. Service symptoms
are attributed to the target city, not to the traffic probe that observed them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np

from aiops_v2.detection.direct_evidence import DirectEvidence
from aiops_v2.events.decoder import DecoderConfig, DecoderEvidence, DecodedEvent, decode_events


def _neighbor_supported(values: np.ndarray, threshold: float = 2.5) -> np.ndarray:
    active = values >= threshold
    supported = np.zeros_like(active)
    supported[1:] |= active[1:] & active[:-1]
    supported[:-1] |= active[:-1] & active[1:]
    return supported


def _edge_probability(strength: np.ndarray) -> np.ndarray:
    raw = 1.0 / (1.0 + np.exp(-np.clip((strength - 3.0) / 0.9, -20.0, 20.0)))
    sustained = _neighbor_supported(strength if strength.ndim == 2 else strength[:, None])
    if strength.ndim == 1:
        sustained = sustained[:, 0]
    return np.where(sustained, 0.9 * raw, 0.4 * raw)


def _target_city(edge: Any) -> str | None:
    if getattr(edge, "relation", None) != "traffic":
        return None
    parts = str(getattr(edge, "target", "")).split(":")
    return parts[1] if len(parts) == 3 and parts[0] == "service-group" else None


def decode_city_events(
    direct: DirectEvidence,
    nodes: tuple[str, ...],
    edges: tuple[Any, ...],
    origin: datetime,
    config: DecoderConfig | None = None,
) -> tuple[DecodedEvent, ...]:
    """Decode city-local node and target-service timelines independently."""
    time_count, node_count = direct.node_probability.shape
    if node_count != len(nodes):
        raise ValueError("direct node scores do not match entity registry")
    edge_scores = direct.edge_symptom_scores
    if edge_scores is None:
        edge_scores = np.zeros((time_count, len(edges)), dtype=np.float32)
    if edge_scores.shape != (time_count, len(edges)):
        raise ValueError("direct edge scores do not match entity registry")

    cities = sorted({node.split("-", 1)[0] for node in nodes if "-" in node})
    results: list[DecodedEvent] = []
    for city in cities:
        node_indexes = np.asarray(
            [index for index, node in enumerate(nodes) if node.startswith(f"{city}-")],
            dtype=np.int64,
        )
        edge_indexes = np.asarray(
            [index for index, edge in enumerate(edges) if _target_city(edge) == city],
            dtype=np.int64,
        )
        if not node_indexes.size and not edge_indexes.size:
            continue

        local_node = np.zeros((time_count, node_count), dtype=np.float32)
        local_node[:, node_indexes] = np.max(
            direct.category_scores[:, node_indexes, :], axis=2
        )
        local_node_probability = np.zeros((time_count, node_count), dtype=np.float32)
        local_node_probability[:, node_indexes] = direct.node_probability[:, node_indexes]
        node_probability = (
            np.max(local_node_probability[:, node_indexes], axis=1)
            if node_indexes.size else np.zeros(time_count, dtype=np.float32)
        )

        local_edge = np.zeros((time_count, len(edges)), dtype=np.float32)
        if edge_indexes.size:
            local_edge[:, edge_indexes] = edge_scores[:, edge_indexes]
            local_edge_scores = edge_scores[:, edge_indexes]
            edge_strength = np.max(local_edge_scores, axis=1)
            # Do not let adjacent but unrelated service edges provide one
            # another's persistence support.
            service_probability = np.max(_edge_probability(local_edge_scores), axis=1)
        else:
            edge_strength = np.zeros(time_count, dtype=np.float32)
            service_probability = np.zeros(time_count, dtype=np.float32)
        probabilities = np.maximum(node_probability, service_probability)

        family = np.zeros((time_count, 3), dtype=np.float32)
        family[:, 0] = np.max(local_node, axis=1) if node_indexes.size else 0.0
        family[:, 1] = edge_strength
        observed = np.zeros((time_count, 3), dtype=bool)
        if node_indexes.size:
            observed[:, 0] = np.any(direct.observed[:, node_indexes], axis=1)
        if edge_indexes.size:
            observed[:, 1] = np.any(edge_scores[:, edge_indexes] > 0, axis=1)
        evidence = DecoderEvidence(
            family_z_scores=family,
            family_observed=observed,
            node=local_node,
            edge=local_edge,
        )
        for event in decode_events(probabilities, origin, config, evidence=evidence):
            interval = slice(event.start_index, event.end_index + 1)
            node_peak = float(np.max(node_probability[interval], initial=0.0))
            service_peak = float(np.max(service_probability[interval], initial=0.0))
            results.append(
                DecodedEvent(
                    event.start_index,
                    event.end_index,
                    event.peak_index,
                    event.confidence,
                    event.score,
                    event.start_time,
                    event.end_time,
                    event.evidence_scores,
                    city_id=city,
                    # A city-local node trigger is a strong RCA prior. For a
                    # service-only symptom, retain all cities as possible
                    # upstream causes.
                    root_city_scoped=node_peak >= service_peak and node_peak > 0,
                )
            )
    results.sort(key=lambda event: (event.start_index, event.city_id or "", event.end_index))
    return tuple(results)

"""Masked semantic evidence from tensorized telemetry.

Only measurements of the candidate device can open an event. Remote traffic,
NetFlow and scrape health remain available to RCA as supporting observations.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import warnings


CATEGORY_NAMES = (
    "cpu_pressure", "memory_pressure", "disk_io_pressure", "disk_space_pressure",
    "process_pressure", "link_fault", "routing_fault", "firewall_fault",
)


@dataclass(frozen=True, slots=True)
class DirectEvidence:
    category_names: tuple[str, ...]
    category_scores: np.ndarray  # [T, N, C], dimensionless evidence above reference
    node_probability: np.ndarray  # [T, N]
    global_probability: np.ndarray  # [T]
    observed: np.ndarray  # [T, N]
    feature_audit: dict[str, dict[str, float]]
    edge_symptom_scores: np.ndarray | None = None  # [T, E], never a direct root
    service_probability: np.ndarray | None = None  # [T], separate from node probability


def select_specific_category(strengths: np.ndarray, names: tuple[str, ...] = CATEGORY_NAMES) -> str | None:
    """Prefer a sustained organ-specific cause over a generic CPU co-symptom.

    A simultaneous CPU rise is expected when a disk saturates or memory is
    scarce. Specific anchors must exceed their own physical/deviation gate;
    generic CPU wins when those anchors are absent.
    """
    values = {name: float(strengths[index]) for index, name in enumerate(names)}
    specific = {
        name: values[name]
        for name, minimum in (("disk_io_pressure", 5.0), ("memory_pressure", 4.0),
                              ("disk_space_pressure", 4.0))
        if values.get(name, 0.0) >= minimum
    }
    if specific:
        return max(specific, key=specific.get)
    if not values or max(values.values()) < 3.0:
        return None
    return max(values, key=values.get)


@dataclass(frozen=True, slots=True)
class _Semantic:
    category: str
    direction: int
    floor: float
    minimum: float | None = None
    maximum: float | None = None
    weight: float = 1.0
    immediate: bool = False


def _semantic(name: str) -> _Semantic | None:
    if name == "node.cpu_usage":
        return _Semantic("cpu_pressure", 1, 4.0, minimum=18.0)
    if name in {"node.load1", "node.load5"}:
        return _Semantic("cpu_pressure", 1, 1.5, weight=0.35)
    if name == "node.memory_available_ratio":
        return _Semantic("memory_pressure", -1, 0.015, maximum=0.90)
    if name == "node.swap_used_ratio":
        return _Semantic("memory_pressure", 1, 0.03, weight=0.50)
    if name == "node.disk_io_util":
        return _Semantic("disk_io_pressure", 1, 10.0, minimum=55.0)
    if name in {"node.disk_read_rate", "node.disk_write_rate"}:
        return _Semantic("disk_io_pressure", 1, 1.0, weight=0.25)
    if name in {"node.filesystem_used_ratio", "node.inode_used_ratio"}:
        return _Semantic("disk_space_pressure", 1, 0.03, minimum=0.8)
    if name == "node.process_count":
        return _Semantic("process_pressure", 1, 10.0)
    if name in {"interface.carrier_changes", "interface.rx_drop_rate", "interface.tx_drop_rate",
                "interface.rx_error_rate", "interface.tx_error_rate"}:
        return _Semantic("link_fault", 1, 0.1, minimum=0.1, immediate="carrier" in name)
    if name in {"routing.bgp_peer_up", "routing.ospf6_interface_enabled",
                "routing.ipv6_route_exists"}:
        return _Semantic("routing_fault", -1, 0.2, immediate=True)
    if name == "routing.ospf6_neighbor_state_code":
        return _Semantic("routing_fault", -1, 0.5, immediate=True)
    if name in {"routing.bgp_peer_prefix_received", "routing.ipv6_route_count"}:
        return _Semantic("routing_fault", -1, 5.0, weight=0.6)
    if name == "routing.bgp_command_success":
        return _Semantic("routing_fault", -1, 0.2, immediate=True)
    return None


def _reference(values: np.ndarray, mask: np.ndarray, floor: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-device reference; missing cells never contribute or become zeroes."""
    counts = mask.sum(axis=0)
    clean = np.where(mask, values, np.nan)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        center = np.nanmedian(clean, axis=0)
        mad = np.nanmedian(np.abs(clean - center[None, :]), axis=0)
    center = np.where(np.isfinite(center), center, 0.0)
    scale = np.maximum(np.nan_to_num(1.4826 * mad, nan=0.0), floor)
    return center, scale, counts


def _neighbor_support(strength: np.ndarray, threshold: float = 2.5) -> np.ndarray:
    active = strength >= threshold
    previous = np.zeros_like(active)
    following = np.zeros_like(active)
    previous[1:] = active[:-1]
    following[:-1] = active[1:]
    return active & (previous | following)


def _service_symptoms(store, time_count: int) -> np.ndarray:
    """Request-aware service-quality evidence; traffic volume alone is ignored."""
    edge_values = getattr(store, "edge_values", None)
    if edge_values is None:
        return np.zeros((time_count, 0), dtype=np.float32)
    edge_count = edge_values.shape[1]
    scores = np.zeros((time_count, edge_count), dtype=np.float32)
    names = store.features.names("edge")
    indexes = {name: index for index, name in enumerate(names)}
    traffic_edges = np.asarray(
        [edge.relation == "traffic" for edge in store.entities.edges], dtype=bool
    )
    if not traffic_edges.any():
        return scores
    for feature_index, name in enumerate(names):
        if not name.startswith("traffic."):
            continue
        flow = name.rsplit(".", 1)[0]
        quality = name.rsplit(".", 1)[-1]
        valid = np.asarray(store.edge_mask[:, :, feature_index], dtype=bool).copy()
        data = np.asarray(edge_values[:, :, feature_index], dtype=np.float32)
        if quality in {"error_rate", "success_rate"}:
            denominator_index = indexes.get(f"{flow}.requests_rate")
            if denominator_index is None:
                continue
            requests = np.asarray(edge_values[:, :, denominator_index], dtype=np.float32)
            request_mask = np.asarray(store.edge_mask[:, :, denominator_index], dtype=bool)
            # Rates and requests are summed over series by the feature store.
            # Precomputed ratio cells are max-aggregated and cannot be paired
            # with the summed denominator without inventing failures.
            failures = data if quality == "error_rate" else requests - data
            adjusted = np.clip(failures, 0.0, np.maximum(requests, 0.0)) / (
                np.maximum(requests, 0.0) + 30.0
            )
            strength = 15.0 * adjusted
            valid &= request_mask & (requests > 0)
        elif quality in {"latency_p95_seconds", "loss_rate"}:
            floor = 0.25 if quality == "latency_p95_seconds" else 0.03
            minimum = 0.5 if quality == "latency_p95_seconds" else 0.05
            center, scale, counts = _reference(data, valid, floor)
            strength = np.maximum((data - center[None, :]) / scale[None, :], 0.0)
            valid &= (data >= minimum) & (counts[None, :] >= 3)
        else:
            continue
        strength = np.where(valid & traffic_edges[None, :], strength, 0.0)
        scores = np.maximum(scores, np.minimum(strength, 16.0))
    return scores


def score_direct_evidence(store) -> DirectEvidence:
    """Return direct device fault evidence without case labels or count priors."""
    values = store.node_values
    mask = store.node_mask
    if values.ndim != 3 or mask.shape != values.shape:
        raise ValueError("node tensors and masks must have matching [T,N,F] shape")
    time_count, node_count, feature_count = values.shape
    categories = np.zeros((time_count, node_count, len(CATEGORY_NAMES)), dtype=np.float32)
    immediate = np.zeros((time_count, node_count), dtype=bool)
    observed = np.any(mask, axis=2)
    audit: dict[str, dict[str, float]] = {}
    names = store.features.names("node")
    if len(names) != feature_count:
        raise ValueError("feature registry does not match node tensor")
    for feature_index, name in enumerate(names):
        semantic = _semantic(name)
        if semantic is None:
            continue
        data = np.asarray(values[:, :, feature_index], dtype=np.float32)
        valid = np.asarray(mask[:, :, feature_index], dtype=bool) & np.isfinite(data)
        center, scale, counts = _reference(data, valid, semantic.floor)
        enough = counts >= 3
        deviation = semantic.direction * (data - center[None, :]) / scale[None, :]
        strength = np.maximum(deviation, 0.0)
        if semantic.minimum is not None:
            strength = np.where(data >= semantic.minimum, strength, 0.0)
        if semantic.maximum is not None:
            strength = np.where(data <= semantic.maximum, strength, 0.0)
        strength = np.where(valid & enough[None, :], strength, 0.0)
        strength = np.minimum(strength * semantic.weight, 16.0 if semantic.weight >= 1 else 2.0)
        index = CATEGORY_NAMES.index(semantic.category)
        categories[:, :, index] = np.maximum(categories[:, :, index], strength)
        if semantic.immediate:
            immediate |= strength >= 3.0
        audit[name] = {
            "observed_cells": int(np.sum(valid)),
            "active_cells": int(np.sum(strength >= 2.5)),
            "maximum_strength": float(np.max(strength, initial=0.0)),
        }
    # FRR is a discrete event source. It is not required to be present in every
    # case; when present, a high-severity message can support an immediate fault.
    if getattr(store, "log_values", None) is not None and store.log_values.shape[-1]:
        for index, name in enumerate(store.features.names("log")):
            if not name.startswith("frr."):
                continue
            if not any(token in name for token in ("err", "crit", "alert", "warning")):
                continue
            data = np.asarray(store.log_values[:, :, index])
            active = np.asarray(store.log_mask[:, :, index]) & (data > 0)
            categories[:, :, CATEGORY_NAMES.index("routing_fault")] = np.maximum(
                categories[:, :, CATEGORY_NAMES.index("routing_fault")],
                np.where(active, 5.0, 0.0),
            )
            immediate |= active
            observed |= np.asarray(store.log_mask[:, :, index])
    strongest = np.max(categories, axis=2)
    sustained = _neighbor_support(strongest)
    # A one-minute gauge excursion is not enough by itself, but a very strong
    # state transition or corroborated neighboring minute is allowed.
    raw = 1.0 / (1.0 + np.exp(-np.clip((strongest - 3.0) / 0.9, -20.0, 20.0)))
    node_probability = np.where(sustained | immediate, raw, raw * 0.55)
    node_probability = np.where(observed, node_probability, 0.0).astype(np.float32)
    edge_symptoms = _service_symptoms(store, time_count)
    global_probability = np.max(node_probability, axis=1) if node_count else np.zeros(time_count)
    edge_probability = np.zeros(time_count, dtype=np.float32)
    if edge_symptoms.shape[1]:
        edge_strength = np.max(edge_symptoms, axis=1)
        edge_raw = 1.0 / (1.0 + np.exp(-np.clip((edge_strength - 3.0) / 0.9, -20.0, 20.0)))
        edge_supported = _neighbor_support(edge_strength[:, None])[:, 0]
        edge_probability = np.where(edge_supported, 0.9 * edge_raw, 0.4 * edge_raw).astype(np.float32)
        global_probability = np.maximum(global_probability, edge_probability)
    return DirectEvidence(
        CATEGORY_NAMES,
        categories,
        node_probability,
        np.asarray(global_probability, dtype=np.float32),
        observed,
        audit,
        edge_symptoms,
        edge_probability,
    )

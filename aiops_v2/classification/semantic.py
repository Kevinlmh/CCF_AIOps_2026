"""Semantic bootstrap classifier constrained by the official taxonomy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import torch
from torch import nn

from aiops_v2.data.feature_store import FeatureStore
from aiops_v2.detection.direct_evidence import RESOURCE_ANCHOR_BOUNDS, RESOURCE_ANCHOR_MIN_CHANGES
from aiops_v2.events.decoder import DecodedEvent


SEMANTIC_SIGNAL_NAMES = (
    "dns_record",
    "rule_order",
    "port_block",
    "acl_drop",
    "rate_limit",
    "default_route",
    "static_route",
    "blackhole",
    "bgp_flap",
    "ospf_cost",
    "port_selectivity",
    "cpu",
    "memory",
    "disk_io",
    "disk_space",
    "process",
    "softirq",
    "bgp_down",
    "ospf_down",
    "link_loss",
    "web_error",
    "web_latency",
    "auth_timeout",
    "auth_latency",
    "auth_error",
    "dns_error",
    "link_delay",
)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    category: dict[str, str]
    confidence: float
    top3: tuple[tuple[str, float], ...]
    signals: dict[str, float]
    candidate_category_scores: dict[str, dict[str, float]] = field(default_factory=dict)


def _deviation(values: np.ndarray, mask: np.ndarray, start: int, end: int, *, low: bool) -> float:
    baseline_start = max(0, start - 30)
    scores = []
    for entity in range(values.shape[1]):
        during_values = values[start:end, entity][mask[start:end, entity]]
        baseline_values = values[baseline_start:start, entity][mask[baseline_start:start, entity]]
        if during_values.size == 0 or baseline_values.size == 0:
            continue
        center = float(np.median(baseline_values))
        q25, q75 = np.percentile(baseline_values, [25.0, 75.0])
        scale = max(float(q75 - q25), abs(center) * 0.05, 0.01)
        change = center - float(np.min(during_values)) if low else float(np.max(during_values)) - center
        scores.append(min(25.0, max(0.0, change / scale)))
    return max(scores, default=0.0)


def _semantic_strength(
    feature: str, values: np.ndarray, mask: np.ndarray,
    start: int, end: int, *, low: bool,
) -> float:
    bound = RESOURCE_ANCHOR_BOUNDS.get(feature)
    change_floor = RESOURCE_ANCHOR_MIN_CHANGES.get(feature)
    eligible = np.asarray(mask, dtype=bool)
    if bound is not None or change_floor is not None:
        eligible = eligible.copy()
        baseline_start = max(0, start - 30)
        for entity in range(values.shape[1]):
            during = values[start:end, entity][eligible[start:end, entity]]
            if during.size == 0:
                continue
            allowed = True
            if bound is not None:
                kind, cutoff = bound
                allowed = (
                    float(np.max(during)) >= cutoff if kind == "minimum"
                    else float(np.min(during)) <= cutoff
                )
            if change_floor is not None:
                baseline = values[baseline_start:start, entity][eligible[baseline_start:start, entity]]
                change = (
                    float(np.median(baseline) - np.min(during)) if low
                    else float(np.max(during) - np.median(baseline))
                ) if baseline.size else 0.0
                allowed &= baseline.size > 0 and change >= change_floor
            if not allowed:
                eligible[start:end, entity] = False
    strength = _deviation(values, eligible, start, end, low=low)
    if feature == "node.disk_io_util":
        during = values[start:end][eligible[start:end]]
        if during.size == 0:
            return 0.0
        # Disk utilization is a percentage. A 0.1 -> 1.0 change may have a
        # large relative score but occupies only 1% of the physical range.
        return strength * min(1.0, max(0.0, float(np.max(during))) / 100.0)
    return strength


def _signal_name(feature: str) -> tuple[str, bool] | None:
    name = feature.lower()
    if any(token in name for token in ("wrong_record", "nxdomain", "servfail")):
        return "dns_record", False
    if "rule_order" in name:
        return "rule_order", False
    if any(token in name for token in ("port_block", "connection_refused", "blocked_port")):
        return "port_block", False
    if any(token in name for token in ("acl", "access_list", "access-list", "policy_drop")):
        return "acl_drop", False
    if "rate_limit" in name or "policer" in name:
        return "rate_limit", False
    if "default_route" in name:
        return "default_route", False
    if "static_route" in name:
        return "static_route", False
    if "blackhole" in name or "unreachable" in name:
        return "blackhole", False
    if "bgp" in name and any(token in name for token in ("flap", "route_change", "update_count")):
        return "bgp_flap", False
    if "ospf" in name and "cost" in name:
        return "ospf_cost", False
    if "unique_dst_port" in name or "unique_destination_port" in name:
        return "port_selectivity", False
    if "cpu" in name and "softirq" not in name:
        return "cpu", False
    if "memory_available" in name or "swap" in name:
        return "memory", "memory_available" in name
    if "disk_io" in name:
        return "disk_io", False
    # Read/write rates have no universal saturation scale. The direct
    # detector retains them as auxiliary evidence, but they cannot create a
    # disk-pressure category by themselves after category normalization.
    if "disk_read" in name or "disk_write" in name:
        return None
    if "filesystem" in name or "inode" in name:
        return "disk_space", False
    if "process_count" in name:
        return "process", False
    if "softirq" in name:
        return "softirq", False
    if "bgp" in name:
        return "bgp_down", any(token in name for token in ("up", "success", "enabled"))
    if "ospf" in name:
        return "ospf_down", any(token in name for token in ("up", "state", "neighbor"))
    if "drop" in name or "loss" in name or "error_rate" in name and "traffic." not in name:
        return "link_loss", False
    if "traffic.web" in name and "error" in name:
        return "web_error", False
    if "traffic.web" in name and "latency" in name:
        return "web_latency", False
    if "traffic.auth" in name and "timeout" in name:
        return "auth_timeout", False
    if "traffic.auth" in name and "error" in name:
        return "auth_error", False
    if "traffic.auth" in name and "latency" in name:
        return "auth_latency", False
    if "traffic.dns" in name and ("error" in name or "success" in name):
        return "dns_error", "success" in name
    if "latency" in name or "jitter" in name:
        return "link_delay", False
    # A throughput or byte/packet-rate decline is a symptom shared by many
    # faults, not direct evidence that a limiter or policer was applied.
    return None


def _extract_signals(
    event: DecodedEvent,
    root_node: str,
    store: FeatureStore,
) -> dict[str, float]:
    start = max(0, event.start_index)
    end = min(int(store.manifest["minute_count"]), event.end_index + 1)
    context_start = max(0, start - 30)
    # Use only evidence connected to the selected target city. Without this
    # restriction, one city's unrelated traffic spike could classify another
    # city's event.
    city = root_node.split("-", 1)[0]
    city_nodes = [
        index
        for index, node in enumerate(store.entities.nodes)
        if node.startswith(f"{city}-")
    ]
    city_edges = [
        index
        for index, edge in enumerate(store.entities.edges)
        if (
            edge.relation == "traffic"
            and edge.target.startswith(f"service-group:{city}:")
        )
        or (edge.relation == "netflow" and edge.source.startswith(f"{city}-"))
    ]
    signals: dict[str, float] = {}
    groups = (
        (store.node_values, store.node_mask, store.features.names("node"), city_nodes),
        (store.edge_values, store.edge_mask, store.features.names("edge"), city_edges),
        (store.log_values, store.log_mask, store.features.names("log"), city_nodes),
    )
    for values, mask, names, entity_indexes in groups:
        for index, feature in enumerate(names):
            semantic = _signal_name(feature)
            if semantic is None:
                continue
            signal, low = semantic
            selected_values = np.asarray(values[context_start:end, entity_indexes, index])
            selected_mask = np.asarray(mask[context_start:end, entity_indexes, index])
            strength = _semantic_strength(
                feature, selected_values, selected_mask,
                start - context_start, end - context_start, low=low,
            )
            signals[signal] = max(signals.get(signal, 0.0), strength)
    return signals


def extract_candidate_signals(
    event: DecodedEvent,
    candidate_node: str,
    store: FeatureStore,
    *,
    edge_indexes: tuple[int, ...] | None = None,
) -> dict[str, float]:
    """Extract semantic raw-feature evidence for one legal node candidate."""
    node_index = store.entities.node_index(candidate_node)
    start = max(0, event.start_index)
    end = min(int(store.manifest["minute_count"]), event.end_index + 1)
    if start >= end:
        raise ValueError("event does not overlap the feature store")
    context_start = max(0, start - 30)
    city = candidate_node.split("-", 1)[0]
    is_service = "-service-vm-" in candidate_node
    selected_edges = edge_indexes
    if selected_edges is None:
        selected_edges = tuple(
            index
            for index, edge in enumerate(store.entities.edges)
            if (
                is_service
                and edge.relation == "traffic"
                and edge.target.startswith(f"service-group:{city}:")
            )
            or (edge.relation == "netflow" and edge.source == candidate_node)
        )
    result = {name: 0.0 for name in SEMANTIC_SIGNAL_NAMES}
    groups = (
        (
            store.node_values[context_start:end, node_index : node_index + 1, :],
            store.node_mask[context_start:end, node_index : node_index + 1, :],
            store.features.names("node"),
        ),
        (
            store.edge_values[context_start:end, selected_edges, :],
            store.edge_mask[context_start:end, selected_edges, :],
            store.features.names("edge"),
        ),
        (
            store.log_values[context_start:end, node_index : node_index + 1, :],
            store.log_mask[context_start:end, node_index : node_index + 1, :],
            store.features.names("log"),
        ),
    )
    for values, mask, names in groups:
        for feature_index, feature in enumerate(names):
            semantic = _signal_name(feature)
            if semantic is None:
                continue
            signal, low = semantic
            strength = _semantic_strength(
                feature, np.asarray(values[:, :, feature_index]),
                np.asarray(mask[:, :, feature_index]),
                start - context_start,
                end - context_start,
                low=low,
            )
            result[signal] = max(result[signal], strength)
    return result


def _category_scores(
    signals: dict[str, float],
    root_node: str,
    categories: list[dict[str, str]],
) -> dict[str, float]:
    scores = {item["fault_name"]: 0.0 for item in categories}
    is_firewall = root_node.endswith("-fw")
    is_router = "-br-" in root_node or "-cr-" in root_node
    cpu_target = "firewall_cpu_pressure" if is_firewall else "resource_cpu_high"
    scores[cpu_target] += signals.get("cpu", 0.0)
    scores["resource_memory_pressure"] += signals.get("memory", 0.0)
    scores["resource_disk_io_pressure"] += signals.get("disk_io", 0.0)
    scores["resource_disk_space_low"] += signals.get("disk_space", 0.0)
    scores["resource_process_pressure"] += signals.get("process", 0.0)
    scores["resource_softirq_udp_pressure"] += signals.get("softirq", 0.0)
    if is_router:
        scores["route_bgp_session_down"] += signals.get("bgp_down", 0.0)
        scores["route_ospf6_neighbor_down"] += signals.get("ospf_down", 0.0)
        scores["route_blackhole"] += signals.get("blackhole", 0.0)
        scores["route_bgp_route_flap"] += signals.get("bgp_flap", 0.0)
        scores["route_wrong_static_route"] += signals.get("static_route", 0.0)
        scores["route_ospf6_cost_anomaly"] += signals.get("ospf_cost", 0.0)
        scores["route_wrong_default_route"] += signals.get("default_route", 0.0)
    scores["link_loss"] += signals.get("link_loss", 0.0)
    scores["link_delay"] += signals.get("link_delay", 0.0)
    scores["link_rate_limit"] += signals.get("rate_limit", 0.0)
    scores["service_web_5xx"] += signals.get("web_error", 0.0)
    scores["service_web_slow"] += signals.get("web_latency", 0.0)
    scores["service_auth_timeout"] += max(
        signals.get("auth_timeout", 0.0), signals.get("auth_latency", 0.0)
    )
    scores["service_auth_error"] += signals.get("auth_error", 0.0)
    scores["service_dns_down"] += signals.get("dns_error", 0.0)
    scores["service_dns_wrong_record"] += signals.get("dns_record", 0.0)
    if is_firewall:
        scores["firewall_acl_drop"] += signals.get("acl_drop", 0.0)
        scores["firewall_rate_limit"] += signals.get("rate_limit", 0.0)
        scores["firewall_port_block"] += max(
            signals.get("port_block", 0.0), signals.get("port_selectivity", 0.0)
        )
        scores["firewall_default_route_error"] += signals.get("default_route", 0.0)
        scores["firewall_rule_order_error"] += signals.get("rule_order", 0.0)
    return scores


def _signals_from_candidate_evidence(evidence) -> tuple[list[dict[str, float]], dict[str, float]]:
    columns = {name: index for index, name in enumerate(evidence.feature_names)}
    signal_names = tuple(
        name.removeprefix("signal_")
        for name in evidence.feature_names
        if name.startswith("signal_")
    )
    per_candidate = []
    global_signals = {name: 0.0 for name in signal_names}
    for row in evidence.features:
        signals = {
            name: float(row[columns[f"signal_{name}"]])
            for name in signal_names
        }
        per_candidate.append(signals)
        for name, value in signals.items():
            global_signals[name] = max(global_signals[name], value)
    return per_candidate, global_signals


def _normalized_root_weights(ranking, candidate_count: int) -> np.ndarray:
    raw = np.asarray(
        [max(0.0, float(ranking.scores.get(node, 0.0))) for node in ranking.candidate_evidence.candidate_nodes],
        dtype=np.float64,
    )
    if raw.shape != (candidate_count,) or not np.isfinite(raw).all() or raw.sum() <= 0:
        return np.full(candidate_count, 1.0 / candidate_count, dtype=np.float64)
    return raw / raw.sum()


def _semantic_candidate_distribution(
    candidate_nodes: tuple[str, ...],
    candidate_signals: list[dict[str, float]],
    root_weights: np.ndarray,
    categories: list[dict[str, str]],
) -> tuple[np.ndarray, dict[str, dict[str, float]]] | None:
    per_candidate_scores = [
        _category_scores(signals, node, categories)
        for node, signals in zip(candidate_nodes, candidate_signals, strict=True)
    ]
    strengths = np.asarray(
        [max(scores.values(), default=0.0) for scores in per_candidate_scores],
        dtype=np.float64,
    )
    active = strengths > 0
    if not np.any(active):
        return None
    # A high-ranked root with a barely positive semantic signal should not
    # cast a full category vote after per-candidate normalization.
    root_active = root_weights * strengths
    root_active = root_active / root_active.sum() if root_active.sum() else active / active.sum()
    root_distribution = np.zeros(len(categories), dtype=np.float64)
    category_evidence = np.zeros(len(categories), dtype=np.float64)
    candidate_probabilities: dict[str, dict[str, float]] = {}
    for index, (node, scores) in enumerate(
        zip(candidate_nodes, per_candidate_scores, strict=True)
    ):
        total = sum(max(0.0, score) for score in scores.values())
        normalized = {
            item["fault_name"]: (
                max(0.0, scores[item["fault_name"]]) / total if total else 0.0
            )
            for item in categories
        }
        candidate_probabilities[node] = normalized
        if active[index]:
            root_distribution += root_active[index] * np.asarray(
                [normalized[item["fault_name"]] for item in categories],
                dtype=np.float64,
            )
            # Multiple service VM candidates can inherit the same target-city
            # traffic symptom. Its magnitude is evidence once per category,
            # regardless of how many legal candidate IDs share it.
            category_evidence = np.maximum(
                category_evidence,
                np.asarray(
                    [max(0.0, scores[item["fault_name"]]) for item in categories],
                    dtype=np.float64,
                ),
            )
    distribution = 0.5 * root_distribution + 0.5 * category_evidence / category_evidence.sum()
    return distribution, candidate_probabilities


def _ranked_result(
    category_probabilities: np.ndarray,
    categories: list[dict[str, str]],
    signals: dict[str, float],
    candidate_scores: dict[str, dict[str, float]],
) -> ClassificationResult:
    order = np.argsort(-category_probabilities, kind="stable")
    ranked = [
        (categories[index]["fault_name"], float(category_probabilities[index]))
        for index in order
    ]
    by_name = {item["fault_name"]: item for item in categories}
    selected = by_name[ranked[0][0]]
    return ClassificationResult(
        category={
            "major_category": selected["major_category"],
            "sub_category": selected["sub_category"],
        },
        confidence=ranked[0][1],
        top3=tuple(ranked[:3]),
        signals=signals,
        candidate_category_scores=candidate_scores,
    )


def classify_event(
    event: DecodedEvent,
    root_node: str,
    store: FeatureStore,
    taxonomy: dict[str, Any],
    *,
    ranking=None,
    diagnosis_heads: nn.Module | None = None,
    device: str = "cpu",
) -> ClassificationResult:
    """Classify an event using candidate-conditioned evidence when available."""
    categories = taxonomy["fault_categories"]
    if ranking is not None:
        evidence = ranking.candidate_evidence
        candidate_nodes = evidence.candidate_nodes
        candidate_signals, global_signals = _signals_from_candidate_evidence(evidence)
        root_weights = _normalized_root_weights(ranking, len(candidate_nodes))
        if diagnosis_heads is not None:
            if diagnosis_heads.category_count != len(categories):
                raise ValueError("diagnosis-head taxonomy size does not match official taxonomy")
            candidate_features = torch.from_numpy(evidence.features).unsqueeze(0).to(device=device)
            event_features = torch.from_numpy(evidence.event_features).unsqueeze(0).to(device=device)
            candidate_mask = torch.from_numpy(evidence.candidate_mask).unsqueeze(0).to(device=device)
            diagnosis_heads = diagnosis_heads.to(device)
            diagnosis_heads.eval()
            with torch.no_grad():
                _, conditional_logits = diagnosis_heads(
                    candidate_features,
                    event_features,
                    candidate_mask,
                )
                conditional_probabilities = torch.softmax(conditional_logits[0], dim=-1).cpu().numpy()
            learned_marginals = np.sum(
                root_weights[:, None] * conditional_probabilities,
                axis=0,
            )
            semantic = _semantic_candidate_distribution(
                candidate_nodes,
                candidate_signals,
                root_weights,
                categories,
            )
            if semantic is None:
                marginals = learned_marginals
                candidate_probabilities = conditional_probabilities
            else:
                semantic_marginals, semantic_candidates = semantic
                learned_weight = 0.25
                marginals = (
                    learned_weight * learned_marginals
                    + (1.0 - learned_weight) * semantic_marginals
                )
                candidate_probabilities = np.stack(
                    [
                        learned_weight * conditional_probabilities[index]
                        + (1.0 - learned_weight)
                        * np.asarray(
                            [
                                semantic_candidates[node][item["fault_name"]]
                                for item in categories
                            ],
                            dtype=np.float64,
                        )
                        for index, node in enumerate(candidate_nodes)
                    ]
                )
            per_candidate = {
                node: {
                    categories[index]["fault_name"]: float(candidate_probabilities[candidate, index])
                    for index in range(len(categories))
                }
                for candidate, node in enumerate(candidate_nodes)
            }
            return _ranked_result(marginals, categories, global_signals, per_candidate)

        semantic = _semantic_candidate_distribution(
            candidate_nodes,
            candidate_signals,
            root_weights,
            categories,
        )
        if semantic is None:
            return classify_event(event, root_node, store, taxonomy)
        category_values, candidate_scores = semantic
        return _ranked_result(category_values, categories, global_signals, candidate_scores)

    signals = _extract_signals(event, root_node, store)
    scores = _category_scores(signals, root_node, categories)
    is_firewall = root_node.endswith("-fw")
    is_router = "-br-" in root_node or "-cr-" in root_node

    default_name = (
        "firewall_cpu_pressure"
        if is_firewall
        else "route_bgp_session_down"
        if is_router
        else "resource_cpu_high"
    )
    has_semantic_evidence = max(scores.values(), default=0.0) > 0
    if not has_semantic_evidence:
        scores[default_name] = 1e-6
    order = {item["fault_name"]: index for index, item in enumerate(categories)}
    ranked = sorted(scores.items(), key=lambda item: (-item[1], order[item[0]]))
    best_name, best_score = ranked[0]
    by_name = {item["fault_name"]: item for item in categories}
    selected = by_name[best_name]
    total = sum(max(0.0, score) for _, score in ranked[:3])
    confidence = float(best_score / total) if has_semantic_evidence and total > 0 else 0.0
    return ClassificationResult(
        category={
            "major_category": selected["major_category"],
            "sub_category": selected["sub_category"],
        },
        confidence=confidence,
        top3=tuple(ranked[:3]),
        signals=signals,
    )

"""Semantic bootstrap classifier constrained by the official taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from aiops_v2.data.feature_store import FeatureStore
from aiops_v2.events.decoder import DecodedEvent


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    category: dict[str, str]
    confidence: float
    top3: tuple[tuple[str, float], ...]
    signals: dict[str, float]


def _deviation(values: np.ndarray, mask: np.ndarray, start: int, end: int, *, low: bool) -> float:
    during_values = values[start:end][mask[start:end]]
    if during_values.size == 0:
        return 0.0
    baseline_start = max(0, start - 30)
    baseline_values = values[baseline_start:start][mask[baseline_start:start]]
    if baseline_values.size == 0:
        return min(25.0, float(np.max(np.abs(during_values))))
    center = float(np.median(baseline_values))
    q25, q75 = np.percentile(baseline_values, [25.0, 75.0])
    scale = max(float(q75 - q25), abs(center) * 0.05, 0.01)
    change = center - float(np.min(during_values)) if low else float(np.max(during_values)) - center
    return min(25.0, max(0.0, change / scale))


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
    if "disk_io" in name or "disk_read" in name or "disk_write" in name:
        return "disk_io", False
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
    if any(token in name for token in ("throughput", "bytes_rate", "packets_rate", "bandwidth")):
        return "rate_limit", True
    return None


def _extract_signals(
    event: DecodedEvent,
    root_node: str,
    store: FeatureStore,
) -> dict[str, float]:
    start = max(0, event.start_index)
    end = min(int(store.manifest["minute_count"]), event.end_index + 1)
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
            selected_values = np.asarray(values[:, entity_indexes, index])
            selected_mask = np.asarray(mask[:, entity_indexes, index])
            strength = _deviation(selected_values, selected_mask, start, end, low=low)
            signals[signal] = max(signals.get(signal, 0.0), strength)
    return signals


def classify_event(
    event: DecodedEvent,
    root_node: str,
    store: FeatureStore,
    taxonomy: dict[str, Any],
) -> ClassificationResult:
    """Map model-delimited event changes into the official closed taxonomy."""
    signals = _extract_signals(event, root_node, store)
    categories = taxonomy["fault_categories"]
    scores = {item["fault_name"]: 0.0 for item in categories}
    is_firewall = root_node.endswith("-fw")

    cpu_target = "firewall_cpu_pressure" if is_firewall else "resource_cpu_high"
    scores[cpu_target] += signals.get("cpu", 0.0)
    scores["resource_memory_pressure"] += signals.get("memory", 0.0)
    scores["resource_disk_io_pressure"] += signals.get("disk_io", 0.0)
    scores["resource_disk_space_low"] += signals.get("disk_space", 0.0)
    scores["resource_process_pressure"] += signals.get("process", 0.0)
    scores["resource_softirq_udp_pressure"] += signals.get("softirq", 0.0)
    scores["route_bgp_session_down"] += signals.get("bgp_down", 0.0)
    scores["route_ospf6_neighbor_down"] += signals.get("ospf_down", 0.0)
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
    if any(token in root_node for token in ("-br-", "-cr-")):
        scores["route_blackhole"] += signals.get("blackhole", 0.0)
        scores["route_bgp_route_flap"] += signals.get("bgp_flap", 0.0)
        scores["route_wrong_static_route"] += signals.get("static_route", 0.0)
        scores["route_ospf6_cost_anomaly"] += signals.get("ospf_cost", 0.0)
        scores["route_wrong_default_route"] += signals.get("default_route", 0.0)

    default_name = (
        "firewall_cpu_pressure"
        if is_firewall
        else "route_bgp_session_down"
        if any(token in root_node for token in ("-br-", "-cr-"))
        else "resource_cpu_high"
    )
    if max(scores.values(), default=0.0) <= 0:
        scores[default_name] = 1e-6
    order = {item["fault_name"]: index for index, item in enumerate(categories)}
    ranked = sorted(scores.items(), key=lambda item: (-item[1], order[item[0]]))
    best_name, best_score = ranked[0]
    by_name = {item["fault_name"]: item for item in categories}
    selected = by_name[best_name]
    total = sum(max(0.0, score) for _, score in ranked[:3])
    confidence = float(best_score / total) if total > 0 else 0.0
    return ClassificationResult(
        category={
            "major_category": selected["major_category"],
            "sub_category": selected["sub_category"],
        },
        confidence=confidence,
        top3=tuple(ranked[:3]),
        signals=signals,
    )

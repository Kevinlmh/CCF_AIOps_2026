"""Deterministic semantic fault templates for diagnosis-head pretraining."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from aiops_challenge_2026.config import load_public_config
from aiops_v2.classification.semantic import SEMANTIC_SIGNAL_NAMES
from aiops_v2.diagnosis_schema import EVENT_FEATURE_NAMES, ROOT_FEATURE_NAMES


@dataclass(frozen=True, slots=True)
class SyntheticDiagnosisExample:
    candidate_features: np.ndarray
    event_features: np.ndarray
    candidate_mask: np.ndarray
    root_index: int
    category_index: int


_CATEGORY_SIGNAL = {
    "link_delay": "link_delay",
    "link_rate_limit": "rate_limit",
    "link_loss": "link_loss",
    "firewall_acl_drop": "acl_drop",
    "firewall_rate_limit": "rate_limit",
    "firewall_port_block": "port_block",
    "firewall_cpu_pressure": "cpu",
    "firewall_default_route_error": "default_route",
    "firewall_rule_order_error": "rule_order",
    "resource_cpu_high": "cpu",
    "resource_memory_pressure": "memory",
    "resource_disk_io_pressure": "disk_io",
    "resource_disk_space_low": "disk_space",
    "resource_process_pressure": "process",
    "resource_softirq_udp_pressure": "softirq",
    "route_blackhole": "blackhole",
    "route_bgp_session_down": "bgp_down",
    "route_bgp_route_flap": "bgp_flap",
    "route_wrong_static_route": "static_route",
    "route_ospf6_neighbor_down": "ospf_down",
    "route_ospf6_cost_anomaly": "ospf_cost",
    "route_wrong_default_route": "default_route",
    "service_dns_down": "dns_error",
    "service_dns_wrong_record": "dns_record",
    "service_web_5xx": "web_error",
    "service_web_slow": "web_latency",
    "service_auth_timeout": "auth_timeout",
    "service_auth_error": "auth_error",
}


def _root_role(fault_name: str) -> str:
    if fault_name.startswith("firewall_"):
        return "fw"
    if fault_name.startswith("route_") or fault_name in {
        "link_delay",
        "link_loss",
        "link_rate_limit",
    }:
        return "br-1"
    return "service-vm-1"


def generate_synthetic_diagnosis_data(
    taxonomy: dict[str, Any],
    *,
    examples_per_category: int,
    seed: int,
    root_feature_names: tuple[str, ...],
    event_feature_names: tuple[str, ...],
) -> tuple[SyntheticDiagnosisExample, ...]:
    """Generate varied injected faults; no case IDs, dates, or sample labels enter."""
    if examples_per_category < 1:
        raise ValueError("examples_per_category must be positive")
    if tuple(root_feature_names) != ROOT_FEATURE_NAMES or tuple(event_feature_names) != EVENT_FEATURE_NAMES:
        raise ValueError("feature schema does not match the v2.0 diagnosis contract")
    categories = taxonomy.get("fault_categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError("taxonomy must contain a non-empty fault_categories list")
    fault_names = [item.get("fault_name") for item in categories]
    if any(not isinstance(name, str) for name in fault_names) or len(set(fault_names)) != len(fault_names):
        raise ValueError("taxonomy fault names must be unique strings")
    unsupported = set(fault_names) - set(_CATEGORY_SIGNAL)
    if unsupported:
        raise ValueError(f"taxonomy category has no semantic template: {sorted(unsupported)}")
    if set(SEMANTIC_SIGNAL_NAMES) != {
        name.removeprefix("signal_")
        for name in ROOT_FEATURE_NAMES
        if name.startswith("signal_")
    }:
        raise ValueError("feature schema semantic signals are inconsistent")

    network = load_public_config("network_elements")
    cities = tuple(network["cities"])
    roles = tuple(network["device_roles"])
    candidate_nodes = tuple(f"{city}-{role}" for city in cities for role in roles)
    feature_index = {name: index for index, name in enumerate(ROOT_FEATURE_NAMES)}
    event_index = {name: index for index, name in enumerate(EVENT_FEATURE_NAMES)}
    rng = np.random.default_rng(seed)
    examples: list[SyntheticDiagnosisExample] = []

    for category_index, category in enumerate(categories):
        fault_name = category["fault_name"]
        signal_name = f"signal_{_CATEGORY_SIGNAL[fault_name]}"
        if signal_name not in feature_index:
            raise ValueError(f"feature schema is missing {signal_name}")
        root_role = _root_role(fault_name)
        role_indexes = [
            index
            for index, node in enumerate(candidate_nodes)
            if node.endswith(f"-{root_role}")
        ]
        if not role_indexes:
            raise ValueError(f"network candidates do not provide root role {root_role}")
        for _ in range(examples_per_category):
            candidate_features = np.zeros(
                (len(candidate_nodes), len(ROOT_FEATURE_NAMES)),
                dtype=np.float32,
            )
            candidate_features[:, feature_index["direct"]] = rng.uniform(
                0.0, 0.25, size=len(candidate_nodes)
            )
            candidate_features[:, feature_index["traffic_target"]] = rng.uniform(
                0.0, 0.8, size=len(candidate_nodes)
            )
            candidate_features[:, feature_index["role_service"]] = np.asarray(
                ["-service-vm-" in node for node in candidate_nodes], dtype=np.float32
            )
            candidate_features[:, feature_index["role_firewall"]] = np.asarray(
                [node.endswith("-fw") for node in candidate_nodes], dtype=np.float32
            )
            candidate_features[:, feature_index["role_router"]] = np.asarray(
                ["-br-" in node or "-cr-" in node for node in candidate_nodes],
                dtype=np.float32,
            )
            candidate_features[:, feature_index["role_traffic_observer"]] = np.asarray(
                [node.endswith("-traffic-vm") for node in candidate_nodes],
                dtype=np.float32,
            )
            root_index = int(rng.choice(role_indexes))
            direct_strength = float(rng.uniform(2.0, 12.0))
            candidate_features[root_index, feature_index["direct"]] = direct_strength
            candidate_features[root_index, feature_index["direct_early"]] = direct_strength * float(
                rng.uniform(0.55, 1.0)
            )
            candidate_features[root_index, feature_index["direct_pre"]] = direct_strength * float(
                rng.uniform(0.0, 0.35)
            )
            candidate_features[root_index, feature_index["direct_persistence"]] = float(
                rng.uniform(0.45, 1.0)
            )
            candidate_features[root_index, feature_index["recovery"]] = direct_strength * float(
                rng.uniform(0.15, 0.65)
            )
            candidate_features[root_index, feature_index["log"]] = float(
                rng.uniform(0.0, 4.0)
            )
            candidate_features[root_index, feature_index["observed"]] = 1.0
            candidate_features[root_index, feature_index[signal_name]] = float(
                rng.uniform(3.0, 10.0)
            )
            # A symptom observer may have conspicuous target traffic, but less
            # direct evidence than the injected root.
            observer_indexes = [
                index
                for index, node in enumerate(candidate_nodes)
                if node.endswith("-traffic-vm") and index != root_index
            ]
            if observer_indexes and rng.random() < 0.75:
                observer = int(rng.choice(observer_indexes))
                candidate_features[observer, feature_index["traffic_target"]] = float(
                    rng.uniform(1.0, 18.0)
                )
                candidate_features[observer, feature_index["observed"]] = 1.0

            event_features = np.zeros(len(EVENT_FEATURE_NAMES), dtype=np.float32)
            event_features[event_index["duration_minutes"]] = float(rng.integers(1, 31))
            event_features[event_index["confidence"]] = float(rng.uniform(0.7, 0.99))
            event_features[event_index["decoder_score"]] = float(rng.uniform(0.5, 10.0))
            family = category["major_category"]
            family_peak = {
                "resource": rng.uniform(2.0, 10.0),
                "link": rng.uniform(1.0, 8.0),
                "firewall": rng.uniform(1.0, 8.0),
                "routing": rng.uniform(1.0, 8.0),
                "service": rng.uniform(1.0, 8.0),
            }[family]
            event_features[event_index["family_node_peak"]] = float(family_peak)
            event_features[event_index["family_edge_peak"]] = float(
                rng.uniform(1.0, 8.0) if family in {"link", "service"} else rng.uniform(0, 2)
            )
            event_features[event_index["family_log_peak"]] = float(
                rng.uniform(1.0, 8.0) if family in {"firewall", "routing"} else rng.uniform(0, 2)
            )
            examples.append(
                SyntheticDiagnosisExample(
                    candidate_features=candidate_features,
                    event_features=event_features,
                    candidate_mask=np.ones(len(candidate_nodes), dtype=bool),
                    root_index=root_index,
                    category_index=category_index,
                )
            )
    return tuple(examples)

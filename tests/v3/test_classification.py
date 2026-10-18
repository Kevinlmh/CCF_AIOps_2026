"""Tests for the T5 mechanism matrix and clear-event fast path."""

from dataclasses import dataclass

import pytest

from aiops_v3.classification import (
    FAMILY_RULES,
    evidence_family,
    is_clear_event,
    mechanism_compatible,
    root_type_matches,
)


@dataclass(frozen=True)
class FakeSignal:
    node: str
    feature: str
    category: str
    role: str = "direct"


@dataclass(frozen=True)
class FakeCandidate:
    node: str
    score: float


@dataclass(frozen=True)
class FakePack:
    signals: tuple[FakeSignal, ...]
    candidates: tuple[FakeCandidate, ...]


@dataclass(frozen=True)
class FakeRules:
    roots: tuple[str, ...]
    category: tuple[str, str]


# --- v4 regressions -------------------------------------------------------


def test_traffic_metric_cannot_justify_cpu_category():
    assert mechanism_compatible(
        "resource", "cpu_pressure", {"traffic"}, "beida-service-vm-1"
    ) == "mechanism_missing"


def test_full_traffic_feature_cannot_justify_cpu_category():
    assert mechanism_compatible(
        "resource", "cpu_pressure", {"traffic.dns.error_ratio"}, "beida-service-vm-1"
    ) == "mechanism_missing"


def test_service_category_requires_service_root():
    assert mechanism_compatible(
        "service", "dns_down", {"netflow"}, "beida-br-1"
    ) == "root_type_mismatch"


def test_service_category_accepts_all_service_vm_instances():
    for instance in (1, 2, 3):
        root = f"beida-service-vm-{instance}"
        assert mechanism_compatible("service", "dns_down", {"netflow"}, root) is None


def test_netflow_path_evidence_supports_link_loss_for_router_root():
    assert mechanism_compatible("link", "loss", {"netflow"}, "xian-br-1") is None


# --- resource family map --------------------------------------------------


RESOURCE_EXPECTED = {
    "cpu_pressure": frozenset({"node.cpu_usage"}),
    "memory_pressure": frozenset({"node.memory_available_ratio"}),
    "disk_io_pressure": frozenset({
        "node.disk_io_util",
        "node.disk_read_rate",
        "node.disk_write_rate",
    }),
    "disk_space_low": frozenset({
        "node.filesystem_used_ratio",
        "node.inode_used_ratio",
    }),
    "process_pressure": frozenset({"node.process_count", "node.open_fd_ratio"}),
    "softirq_pressure": frozenset({"node.cpu_usage"}),
}

RESOURCE_FEATURES = {
    "cpu_pressure": "node.cpu_usage",
    "memory_pressure": "node.memory_available_ratio",
    "disk_io_pressure": "node.disk_read_rate",
    "disk_space_low": "node.inode_used_ratio",
    "process_pressure": "node.open_fd_ratio",
    "softirq_pressure": "node.cpu_usage",
}


@pytest.mark.parametrize("sub", sorted(RESOURCE_EXPECTED))
def test_resource_family_map_covers_all_used_subcategories(sub):
    feature = RESOURCE_FEATURES[sub]
    assert FAMILY_RULES[("resource", sub)] == RESOURCE_EXPECTED[sub]
    assert mechanism_compatible(
        "resource", sub, {evidence_family(feature)}, "beida-service-vm-1"
    ) is None


@pytest.mark.parametrize("sub", sorted(RESOURCE_EXPECTED))
def test_resource_subcategory_rejects_traffic_only_evidence(sub):
    assert mechanism_compatible(
        "resource", sub, {"traffic"}, "beida-service-vm-1"
    ) == "mechanism_missing"


# --- link / firewall / routing / service rows -----------------------------


LINK_EXPECTED = {
    "loss": frozenset({
        "interface.rx_drop_rate",
        "interface.tx_drop_rate",
        "interface.rx_error_rate",
        "interface.tx_error_rate",
        "interface.carrier_changes",
        "interface.rx_packets_rate",
        "interface.tx_packets_rate",
        "netflow.path.response_ratio",
    }),
    "ndp_failure": frozenset({
        "interface.carrier_changes",
        "interface.rx_error_rate",
        "interface.tx_error_rate",
        "netflow.path.response_ratio",
    }),
    "vxlan_misconfig": frozenset({
        "netflow.path.response_ratio",
        "netflow.bytes.protocol_17",
    }),
    "delay": frozenset({
        "traffic.*.latency_p95_seconds",
        "netflow.path.response_ratio",
    }),
    "rate_limit": frozenset({
        "traffic.*.latency_p95_seconds",
        "netflow.path.response_ratio",
        "interface.rx_packets_rate",
        "interface.tx_packets_rate",
    }),
}


@pytest.mark.parametrize("sub", sorted(LINK_EXPECTED))
def test_link_matrix_matches_plan(sub):
    assert FAMILY_RULES[("link", sub)] == LINK_EXPECTED[sub]


def test_traffic_latency_template_matches_link_delay():
    assert mechanism_compatible(
        "link", "delay", {"traffic.dns.latency_p95_seconds"}, "xian-br-1"
    ) is None
    assert mechanism_compatible(
        "link", "delay", {"traffic.dns.error_ratio"}, "xian-br-1"
    ) == "mechanism_missing"


def test_netflow_feature_specificity_is_honoured_for_full_features():
    assert mechanism_compatible(
        "link", "loss", {"netflow.path.response_ratio"}, "xian-br-1"
    ) is None
    assert mechanism_compatible(
        "link", "loss", {"netflow.service.response_ratio"}, "xian-br-1"
    ) == "mechanism_missing"


def test_firewall_exact_cpu_row_shadows_major_fallback():
    assert mechanism_compatible(
        "firewall", "acl_drop", {"traffic.dns.error_ratio"}, "beida-fw"
    ) is None
    assert mechanism_compatible(
        "firewall", "cpu_pressure", {"traffic.dns.error_ratio"}, "beida-fw"
    ) == "mechanism_missing"


def test_routing_fallback_requires_routing_or_service_netflow_evidence():
    assert mechanism_compatible(
        "routing", "blackhole", {"routing.bgp_peer_up"}, "xian-cr-1"
    ) is None
    assert mechanism_compatible(
        "routing", "blackhole", {"netflow.service.response_ratio"}, "xian-br-1"
    ) is None
    assert mechanism_compatible(
        "routing", "blackhole", {"netflow.path.response_ratio"}, "xian-br-1"
    ) == "mechanism_missing"


def test_routing_requires_router_or_firewall_root():
    assert mechanism_compatible(
        "routing", "blackhole", {"routing.bgp_peer_up"}, "xian-service-vm-1"
    ) == "root_type_mismatch"


def test_service_family_uses_traffic_or_service_netflow():
    assert mechanism_compatible(
        "service", "web_5xx", {"traffic.web.error_ratio"}, "beida-service-vm-2"
    ) is None
    assert mechanism_compatible(
        "service", "web_5xx", {"netflow.service.response_ratio"}, "beida-service-vm-2"
    ) is None
    assert mechanism_compatible(
        "service", "web_5xx", {"interface.rx_drop_rate"}, "beida-service-vm-2"
    ) == "mechanism_missing"


@pytest.mark.parametrize(
    ("major", "sub"),
    [
        ("resource", "bgp_session_down"),
        ("link", "blackhole"),
        ("link", "wrong_static_route"),
        ("network", "loss"),
    ],
)
def test_unknown_category_pair_returns_mechanism_missing(major, sub):
    assert mechanism_compatible(
        major, sub, {"node", "interface", "routing", "traffic", "netflow"}, "beida-br-1"
    ) == "mechanism_missing"


# --- evidence_family ------------------------------------------------------


@pytest.mark.parametrize(
    ("feature", "expected"),
    [
        ("node.cpu_usage", "node"),
        ("node.disk_read_rate", "node"),
        ("interface.rx_drop_rate", "interface"),
        ("interface.carrier_changes", "interface"),
        ("routing.bgp_peer_up", "routing"),
        ("routing.ospf6_neighbor_up", "routing"),
        ("traffic.dns.error_ratio", "traffic"),
        ("traffic.web.latency_p95_seconds", "traffic"),
        ("netflow.service.response_ratio", "netflow"),
        ("netflow.path.response_ratio", "netflow"),
        ("netflow.bytes.protocol_17", "netflow"),
    ],
)
def test_evidence_family_known_prefixes(feature, expected):
    assert evidence_family(feature) == expected


@pytest.mark.parametrize(
    "feature",
    ["", "unknown.metric", "city:wuhan", "log.auth.error", "nodex.cpu_usage"],
)
def test_evidence_family_unknown_prefixes(feature):
    assert evidence_family(feature) == "unknown"


# --- root_type_matches ----------------------------------------------------


@pytest.mark.parametrize(
    ("major", "root", "expected"),
    [
        ("service", "beida-service-vm-1", True),
        ("service", "beida-service-vm-3", True),
        ("service", "beida-service-vm-4", False),
        ("service", "beida-br-1", False),
        ("firewall", "beida-fw", True),
        ("firewall", "beida-br-1", False),
        ("link", "beida-br-1", True),
        ("link", "beida-cr-2", True),
        ("link", "beida-fw", True),
        ("link", "beida-service-vm-1", False),
        ("link", "beida-traffic-vm", False),
        ("routing", "xian-cr-1", True),
        ("routing", "xian-monitor-vm", False),
        ("resource", "guangzhou-monitor-vm", True),
        ("resource", "not-a-node", False),
        ("network", "beida-br-1", False),
    ],
)
def test_root_type_matches(major, root, expected):
    assert root_type_matches(major, root) is expected


# --- is_clear_event -------------------------------------------------------

CLEAR_NODE = "beida-service-vm-1"


def clear_pack(*, lead=0.16):
    return FakePack(
        signals=(
            FakeSignal(CLEAR_NODE, "node.cpu_usage", "cpu_pressure"),
        ),
        candidates=(
            FakeCandidate(CLEAR_NODE, 1.0),
            FakeCandidate("beida-service-vm-2", 1.0 - lead),
        ),
    )


def clear_rules():
    return FakeRules(
        (CLEAR_NODE, "beida-service-vm-2", "beida-service-vm-3"),
        ("resource", "cpu_pressure"),
    )


def test_is_clear_event_true_for_single_direct_node_with_clear_lead():
    assert is_clear_event(clear_pack(), clear_rules()) is True


def test_is_clear_event_accepts_exact_015_lead():
    assert is_clear_event(clear_pack(lead=0.15), clear_rules()) is True


def test_is_clear_event_false_for_multiple_direct_nodes():
    pack = FakePack(
        signals=(
            FakeSignal(CLEAR_NODE, "node.cpu_usage", "cpu_pressure"),
            FakeSignal("beida-br-1", "node.cpu_usage", "cpu_pressure"),
        ),
        candidates=(
            FakeCandidate(CLEAR_NODE, 1.0),
            FakeCandidate("beida-br-1", 0.8),
        ),
    )
    assert is_clear_event(pack, clear_rules()) is False


def test_is_clear_event_false_for_close_scores():
    assert is_clear_event(clear_pack(lead=0.10), clear_rules()) is False


def test_is_clear_event_false_when_direct_categories_disagree():
    pack = FakePack(
        signals=(
            FakeSignal(CLEAR_NODE, "node.cpu_usage", "cpu_pressure"),
            FakeSignal(CLEAR_NODE, "node.memory_available_ratio", "memory_pressure"),
        ),
        candidates=(
            FakeCandidate(CLEAR_NODE, 1.0),
            FakeCandidate("beida-service-vm-2", 0.8),
        ),
    )
    assert is_clear_event(pack, clear_rules()) is False


def test_is_clear_event_false_without_direct_signals():
    pack = FakePack(
        signals=(
            FakeSignal(CLEAR_NODE, "node.cpu_usage", "cpu_pressure", role="symptom"),
        ),
        candidates=(
            FakeCandidate(CLEAR_NODE, 1.0),
            FakeCandidate("beida-service-vm-2", 0.8),
        ),
    )
    assert is_clear_event(pack, clear_rules()) is False


def test_is_clear_event_false_when_rule_category_fails_mechanism():
    pack = FakePack(
        signals=(
            FakeSignal(CLEAR_NODE, "traffic.dns.error_ratio", "cpu_pressure"),
        ),
        candidates=(
            FakeCandidate(CLEAR_NODE, 1.0),
            FakeCandidate("beida-service-vm-2", 0.8),
        ),
    )
    assert is_clear_event(pack, clear_rules()) is False


def test_is_clear_event_false_when_root_type_does_not_match_category():
    pack = FakePack(
        signals=(
            FakeSignal("beida-br-1", "netflow.service.response_ratio", "service_down"),
        ),
        candidates=(
            FakeCandidate("beida-br-1", 1.0),
            FakeCandidate("beida-fw", 0.8),
        ),
    )
    rules = FakeRules(("beida-br-1",), ("service", "dns_down"))
    assert is_clear_event(pack, rules) is False


def test_is_clear_event_false_when_direct_node_is_not_top_candidate():
    pack = FakePack(
        signals=(
            FakeSignal(CLEAR_NODE, "node.cpu_usage", "cpu_pressure"),
        ),
        candidates=(
            FakeCandidate("beida-service-vm-2", 1.0),
            FakeCandidate(CLEAR_NODE, 0.8),
        ),
    )
    assert is_clear_event(pack, clear_rules()) is False


def test_is_clear_event_false_without_a_second_candidate():
    pack = FakePack(
        signals=(
            FakeSignal(CLEAR_NODE, "node.cpu_usage", "cpu_pressure"),
        ),
        candidates=(FakeCandidate(CLEAR_NODE, 1.0),),
    )
    assert is_clear_event(pack, clear_rules()) is False


def test_is_clear_event_uses_best_score_per_candidate_node():
    pack = FakePack(
        signals=(
            FakeSignal(CLEAR_NODE, "node.cpu_usage", "cpu_pressure"),
        ),
        candidates=(
            FakeCandidate(CLEAR_NODE, 0.9),
            FakeCandidate(CLEAR_NODE, 1.0),
            FakeCandidate("beida-service-vm-2", 0.8),
        ),
    )
    assert is_clear_event(pack, clear_rules()) is True

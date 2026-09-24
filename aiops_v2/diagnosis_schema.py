"""Shared feature names for root ranking and supervised diagnosis heads."""

from aiops_v2.classification.semantic import SEMANTIC_SIGNAL_NAMES


ROOT_FEATURE_NAMES = (
    "direct",
    "direct_early",
    "direct_pre",
    "direct_persistence",
    "recovery",
    "log",
    "traffic_target",
    "traffic_observer",
    "netflow",
    "role_service",
    "role_firewall",
    "role_router",
    "role_traffic_observer",
    "observed",
    *(f"signal_{name}" for name in SEMANTIC_SIGNAL_NAMES),
)
EVENT_FEATURE_NAMES = (
    "duration_minutes",
    "confidence",
    "decoder_score",
    "family_node_peak",
    "family_edge_peak",
    "family_log_peak",
)

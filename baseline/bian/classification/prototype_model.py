"""Closed-set fault classification using auditable signal prototypes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from typing import Any

from ..localization.graph_fusion import RankingResult
from ..preprocessing.observations import AnomalyEvidence, DetectedEvent


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    category: dict[str, str]
    confidence: float
    top3: tuple[dict[str, Any], ...]
    signals: dict[str, float]


def validate_prototypes(taxonomy: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Return taxonomy fault names that have no non-empty configured prototype."""
    prototypes = config.get("prototypes", {})
    return [
        item["fault_name"]
        for item in taxonomy.get("fault_categories", [])
        if not isinstance(prototypes.get(item["fault_name"]), dict)
        or not prototypes[item["fault_name"]]
    ]


def _add_metric_signals(
    signals: dict[str, float],
    point: AnomalyEvidence,
    weight: float,
) -> None:
    metric = point.metric.lower()
    summary = (point.summary or "").lower()
    text = " ".join(
        [metric, summary]
        + [str(value).lower() for _, value in point.dimensions]
    )

    def add(name: str, factor: float = 1.0) -> None:
        signals[name] += weight * factor

    if "cpu" in text:
        add("cpu", 1.35)
    if "load" in text:
        add("cpu", 0.8)
        add("load", 1.0)
    if "memory_available" in text:
        # A sustained loss of available memory is more fault-specific than
        # secondary disk traffic caused by paging or recovery activity.
        add("memory", 5.0)
    if "swap" in text:
        add("memory", 1.8)
        add("swap", 1.0)
    if any(token in text for token in ("disk_io", "disk_read", "disk_write", "io_util")):
        add("disk_io", 1.5)
    if any(token in text for token in ("filesystem", "disk_space", "inode")):
        add("disk_space", 1.8)
    if "process_count" in text or "process_pressure" in text:
        add("process", 1.5)
    if "softirq" in text:
        add("softirq_udp", 2.0)
    if "udp" in metric or "udp" in summary:
        add("udp", 0.8)

    if point.source == "interface":
        add("interface", 0.5)
    if any(token in text for token in ("drop", "loss", "discard")):
        add("loss", 1.6)
    if any(token in text for token in ("carrier", "link_down", "oper_up")):
        add("link_state", 1.7)
    if any(token in text for token in ("bytes_rate", "packets_rate", "throughput", "bandwidth")):
        add("throughput", 0.8)
        if point.direction == "low":
            add("throughput_low", 1.3)
    if "latency" in text or "delay" in text or "rtt" in text:
        add("latency", 1.7)
    if "jitter" in text:
        add("jitter", 1.2)

    if "bgp" in text:
        add("bgp", 1.0)
        if any(token in text for token in ("peer_up", "session_down", "neighbor_down")):
            add("bgp_down", 2.1)
        if any(token in text for token in ("flap", "route_change", "update")):
            add("bgp_flap", 1.8)
    if "ospf" in text:
        add("ospf", 1.0)
        if "cost" in text:
            add("ospf_cost", 2.0)
        if any(token in text for token in ("neighbor", "peer", "down", "up")):
            add("ospf_down", 1.8)
    if "default_route" in text or "default route" in text:
        add("default_route", 2.2)
    if "static_route" in text or "static route" in text:
        add("static_route", 2.0)
    if "blackhole" in text or "unreachable" in text:
        add("blackhole", 2.0)
    if "route_count" in text or "route_total" in text:
        add("route_table", 1.0)

    service_fault_tokens = (
        "error",
        "success",
        "timeout",
        "latency",
        "observed_qps",
        "loss",
        "retransmit",
        "wrong_record",
        "nxdomain",
        "servfail",
        "5xx",
    )
    for service in ("web", "dns", "auth"):
        service_named = f".{service}." in metric or service in summary
        if service_named and any(token in metric or token in summary for token in service_fault_tokens):
            add(service, 1.8)
    if any(token in text for token in ("error_ratio", "5xx", "server_error")):
        add("service_error", 2.0)
    if any(token in text for token in ("timeout", "timed_out")):
        add("timeout", 2.0)
    if "success_ratio" in text and point.direction == "low":
        add("service_error", 1.5)
    if any(token in text for token in ("wrong_record", "nxdomain", "servfail")):
        add("dns_record", 2.0)

    if "unique_dst_port" in text or "unique_destination_port" in text:
        add("port_selectivity", 1.2)
    if "protocol_byte_share" in text:
        add("protocol_selectivity", 1.0)
    if any(token in text for token in ("acl", "access-list", "access_list")):
        add("acl", 2.0)
    if any(token in text for token in ("rule_order", "rule order")):
        add("rule_order", 2.0)
    if any(token in text for token in ("port_block", "connection_refused")):
        add("port_block", 2.0)
    if "rate_limit" in text or "policer" in text:
        add("rate_limit", 2.0)


def _event_signals(
    event: DetectedEvent,
    ranking: RankingResult,
    config: dict[str, Any],
) -> dict[str, float]:
    signals: dict[str, float] = defaultdict(float)
    minute_signal_max: dict[tuple[object, str], float] = {}
    root = ranking.top5[0]["network_element_id"] if ranking.top5 else None
    shortlist_weights = {
        item["network_element_id"]: max(0.03, 0.10 - 0.015 * (item["rank"] - 1))
        for item in ranking.top5[1:]
    }
    for point in event.evidence:
        if root is not None and point.node_id == root:
            node_weight = 5.0
        elif root is not None and root in point.related_node_ids:
            node_weight = 0.10
        else:
            node_weight = shortlist_weights.get(point.node_id or "", 0.01)
        strength = min(1.0, point.score / 15.0)
        if point.event_role == "support":
            strength *= max(0.0, float(config.get("support_signal_weight", 0.25)))
        strength *= 1.0 + max(
            0.0, float(config.get("semantic_signal_bonus", 0.0))
        ) * max(0.0, point.semantic_score)
        point_signals: dict[str, float] = defaultdict(float)
        _add_metric_signals(point_signals, point, node_weight * strength)
        minute = point.timestamp.replace(second=0, microsecond=0)
        for name, value in point_signals.items():
            key = (minute, name)
            minute_signal_max[key] = max(minute_signal_max.get(key, 0.0), value)

    # Metrics such as CPU and load, or disk util/read/write, are correlated
    # measurements of the same underlying condition.  Counting every metric
    # row lets derivative multiplicity and event length dominate the class.
    # Retain the strongest contribution per semantic signal and minute instead.
    for (_, name), value in minute_signal_max.items():
        signals[name] += value

    if ranking.top5:
        role = ranking.by_node[ranking.top5[0]["network_element_id"]].get("device_role", "")
        if role == "fw":
            signals["role_firewall"] += 1.0
        elif role.startswith(("br-", "cr-")):
            signals["role_router"] += 1.0
        elif role.startswith("service-vm"):
            signals["role_service"] += 1.0
        elif role == "traffic-vm":
            signals["role_traffic"] += 1.0
    return dict(signals)


def _cosine(signals: dict[str, float], prototype: dict[str, Any]) -> float:
    dot = sum(signals.get(name, 0.0) * float(value) for name, value in prototype.items())
    left = math.sqrt(sum(value * value for value in signals.values()))
    right = math.sqrt(sum(float(value) ** 2 for value in prototype.values()))
    if left == 0.0 or right == 0.0:
        return 0.0
    return dot / (left * right)


def classify_event(
    event: DetectedEvent,
    ranking: RankingResult,
    taxonomy: dict[str, Any],
    config: dict[str, Any],
) -> ClassificationResult:
    """Classify one detected event into exactly one public taxonomy pair."""
    missing = validate_prototypes(taxonomy, config)
    if missing:
        raise ValueError(f"missing fault prototypes: {', '.join(missing)}")
    signals = _event_signals(event, ranking, config)
    role_prior = float(config.get("role_prior_weight", 0.12))
    conflict_weight = max(0.0, float(config.get("conflict_weight", 0.0)))
    prototypes = config["prototypes"]
    conflicts = config.get("conflicts", {})
    scored: list[tuple[int, dict[str, Any], float]] = []
    for index, item in enumerate(taxonomy["fault_categories"]):
        score = _cosine(signals, prototypes[item["fault_name"]])
        conflict = conflicts.get(item["fault_name"], {})
        if isinstance(conflict, dict) and conflict:
            score -= conflict_weight * _cosine(signals, conflict)
        major = item["major_category"]
        if major == "firewall":
            score += role_prior * min(1.0, signals.get("role_firewall", 0.0))
        elif major == "routing":
            score += role_prior * min(1.0, signals.get("role_router", 0.0))
        elif major == "service":
            score += role_prior * min(
                1.0, signals.get("role_service", 0.0) + signals.get("role_traffic", 0.0)
            )
        scored.append((index, item, max(0.0, score)))
    scored.sort(key=lambda row: (-row[2], row[0]))
    if (
        signals.get("role_firewall", 0.0) > 0.0
        and signals.get("cpu", 0.0) > 0.0
        and scored
        and scored[0][1]["major_category"] == "resource"
        and scored[0][1]["sub_category"] == "cpu_pressure"
    ):
        target = next(
            (
                row
                for row in scored
                if row[1]["major_category"] == "firewall"
                and row[1]["sub_category"] == "cpu_pressure"
            ),
            None,
        )
        if target is not None:
            scored = [row for row in scored if row is not target]
            scored.append((target[0], target[1], scored[0][2] + 1e-6))
            scored.sort(key=lambda row: (-row[2], row[0]))
    best = scored[0]
    runner_up = scored[1][2] if len(scored) > 1 else 0.0
    confidence = 0.0 if best[2] <= 0 else min(1.0, 0.5 * best[2] + 0.5 * (best[2] - runner_up) / best[2])
    top3 = tuple(
        {
            "fault_name": item["fault_name"],
            "major_category": item["major_category"],
            "sub_category": item["sub_category"],
            "score": round(score, 6),
        }
        for _, item, score in scored[:3]
    )
    return ClassificationResult(
        category={
            "major_category": best[1]["major_category"],
            "sub_category": best[1]["sub_category"],
        },
        confidence=round(confidence, 6),
        top3=top3,
        signals={name: round(value, 6) for name, value in sorted(signals.items())},
    )

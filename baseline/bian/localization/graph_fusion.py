"""Topology-aware deterministic root-cause feature fusion."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..preprocessing.observations import AnomalyEvidence, DetectedEvent


@dataclass(frozen=True, slots=True)
class RankingResult:
    top5: list[dict[str, Any]]
    candidates: tuple[dict[str, Any], ...]
    by_node: dict[str, dict[str, Any]]


DIRECT_SOURCE_WEIGHTS = {
    "node": 1.0,
    "routing": 1.0,
    "frr": 1.0,
    "interface": 0.82,
    "netflow": 0.48,
    "traffic": 0.35,
    "scrape": 0.18,
}


def _candidate_ids(network_config: dict[str, Any]) -> list[str]:
    return [
        f"{city}-{role}"
        for city in network_config["cities"]
        for role in network_config["device_roles"]
    ]


def _role(node_id: str, cities: list[str]) -> str:
    for city in cities:
        prefix = city + "-"
        if node_id.startswith(prefix):
            return node_id[len(prefix) :]
    return node_id


def _adjacency(topology: dict[str, Any], candidates: list[str]) -> dict[str, set[str]]:
    result = {node: set() for node in candidates}
    for edge in topology.get("edges", []):
        source = edge.get("source")
        target = edge.get("target")
        if source not in result or target not in result:
            continue
        result[source].add(target)
        result[target].add(source)
    return result


def _distances(start: str, graph: dict[str, set[str]]) -> dict[str, int]:
    if start not in graph:
        return {}
    result = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in graph[node]:
            if neighbor in result:
                continue
            result[neighbor] = result[node] + 1
            queue.append(neighbor)
    return result


def _severity(points: list[AnomalyEvidence]) -> float:
    if not points:
        return 0.0
    scores = sorted((point.score / 25.0 for point in points), reverse=True)[:3]
    strongest = scores[0]
    support = sum(scores[1:]) / max(1, len(scores) - 1)
    return min(1.0, strongest * 0.8 + support * 0.2)


def _persistence(points: list[AnomalyEvidence], duration_minutes: float) -> float:
    if not points:
        return 0.0
    minutes = {point.timestamp.replace(second=0, microsecond=0) for point in points}
    return min(1.0, len(minutes) / max(1.0, duration_minutes))


def _precedence(points: list[AnomalyEvidence], event: DetectedEvent) -> float:
    if not points:
        return 0.0
    earliest = min(point.timestamp for point in points)
    duration = max(60.0, (event.end - event.start).total_seconds())
    delay = max(0.0, (earliest - event.start).total_seconds())
    return max(0.0, 1.0 - delay / duration)


def _source_diversity(points: list[AnomalyEvidence]) -> float:
    sources = {point.source for point in points}
    return min(1.0, len(sources) / 3.0)


def _point_directness(point: AnomalyEvidence) -> float:
    base = DIRECT_SOURCE_WEIGHTS.get(point.source, 0.3)
    metric = point.metric.lower()
    if point.source == "node" and any(
        token in metric
        for token in (
            "cpu",
            "load",
            "memory",
            "swap",
            "disk",
            "filesystem",
            "inode",
            "process",
            "open_fd",
        )
    ):
        return 1.0
    if point.source in {"routing", "frr"}:
        return 1.0
    if point.source == "interface" and any(token in metric for token in ("drop", "error", "carrier")):
        return 0.9
    return base


def _directness(points: list[AnomalyEvidence]) -> float:
    if not points:
        return 0.0
    weighted = sum(_point_directness(point) * point.score for point in points)
    total = sum(point.score for point in points)
    return weighted / total if total > 0 else 0.0


def _relational_support(points: list[AnomalyEvidence]) -> float:
    return min(1.0, _severity(points) * 0.8) if points else 0.0


def _topology_explanation(
    node_id: str,
    direct_by_node: dict[str, list[AnomalyEvidence]],
    graph: dict[str, set[str]],
) -> float:
    distances = _distances(node_id, graph)
    contributions = []
    for observed_node, points in direct_by_node.items():
        # Local anomaly strength is already represented by severity/directness;
        # topology support must explain *other* observed nodes rather than
        # rewarding an isolated candidate for reaching itself at distance zero.
        if observed_node == node_id or observed_node not in distances or not points:
            continue
        contributions.append(_severity(points) / (1.0 + distances[observed_node]))
    if not contributions:
        return 0.0
    contributions.sort(reverse=True)
    return min(1.0, sum(contributions[:5]) / min(3, len(contributions)))


def _serialize_evidence(point: AnomalyEvidence) -> dict[str, Any]:
    return {
        "timestamp_utc": point.timestamp.isoformat().replace("+00:00", "Z"),
        "source": point.source,
        "metric": point.metric,
        "score": round(point.score, 5),
        "value": round(point.value, 8),
        "baseline": round(point.baseline, 8),
        "direction": point.direction,
        "dimensions": dict(point.dimensions),
        "summary": point.summary,
        "event_role": point.event_role,
    }


def rank_candidates(
    event: DetectedEvent,
    network_config: dict[str, Any],
    topology: dict[str, Any],
    config: dict[str, Any],
) -> RankingResult:
    """Rank all valid public elements using local, temporal and graph evidence."""
    all_candidates = _candidate_ids(network_config)
    all_valid = set(all_candidates)
    cities = list(network_config["cities"])
    observed_ids = {
        node_id
        for point in event.evidence
        for node_id in (point.node_id, *point.related_node_ids)
        if node_id in all_valid
    }
    active_cities = {
        city
        for city in cities
        if any(node_id.startswith(city + "-") for node_id in observed_ids)
    }
    candidates = [
        node_id
        for node_id in all_candidates
        if not active_cities
        or any(node_id.startswith(city + "-") for city in active_cities)
    ]
    valid = set(candidates)
    direct_by_node: dict[str, list[AnomalyEvidence]] = defaultdict(list)
    related_by_node: dict[str, list[AnomalyEvidence]] = defaultdict(list)
    for point in event.evidence:
        if point.node_id in valid:
            direct_by_node[point.node_id].append(point)
        for related in point.related_node_ids:
            if related in valid and related != point.node_id:
                related_by_node[related].append(point)

    graph = _adjacency(topology, candidates)
    weights = config.get("weights", {})
    duration_minutes = max(1.0, (event.end - event.start).total_seconds() / 60.0)
    records: list[dict[str, Any]] = []
    for node_id in candidates:
        direct = direct_by_node.get(node_id, [])
        related = related_by_node.get(node_id, [])
        severity = _severity(direct)
        persistence = _persistence(direct, duration_minutes)
        precedence = _precedence(direct, event)
        diversity = _source_diversity(direct)
        directness = _directness(direct)
        relational = _relational_support(related)
        topology_score = _topology_explanation(node_id, direct_by_node, graph)
        symptom_penalty = 0.0
        if related and not direct:
            symptom_penalty = 0.8
        elif direct and directness < 0.5:
            symptom_penalty = 0.35
        components = {
            "severity": severity,
            "persistence": persistence,
            "precedence": precedence,
            "source_diversity": diversity,
            "directness": directness,
            "relational_support": relational,
            "topology_explanation": topology_score,
            "symptom_penalty": symptom_penalty,
        }
        score = sum(float(weights.get(name, 0.0)) * value for name, value in components.items())
        strongest = sorted(direct, key=lambda point: (-point.score, point.timestamp, point.metric))[:8]
        records.append(
            {
                "node_id": node_id,
                "device_role": _role(node_id, cities),
                "score": max(0.0, score),
                **{name: round(value, 6) for name, value in components.items()},
                "anomaly_count": len(direct),
                "related_anomaly_count": len(related),
                "sources": sorted({point.source for point in direct}),
                "first_anomaly": (
                    min(point.timestamp for point in direct).isoformat().replace("+00:00", "Z")
                    if direct
                    else None
                ),
                "evidence": [_serialize_evidence(point) for point in strongest],
            }
        )

    records.sort(
        key=lambda item: (
            -item["score"],
            -item["directness"],
            -item["precedence"],
            item["node_id"],
        )
    )
    for rank, item in enumerate(records, 1):
        item["rank"] = rank
        item["score"] = round(float(item["score"]), 8)
    top5 = [
        {"rank": rank, "network_element_id": item["node_id"]}
        for rank, item in enumerate(records[:5], 1)
    ]
    by_node = {item["node_id"]: item for item in records}
    return RankingResult(top5=top5, candidates=tuple(records), by_node=by_node)

"""Evidence-bound candidate retrieval and deterministic diagnosis."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json

import numpy as np

from .contracts import OfficialContract
from .detection import Event, Signal


@dataclass(frozen=True)
class Candidate:
    node: str
    score: float
    evidence_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class EvidencePack:
    event_id: str
    start_time: str
    end_time: str
    signals: tuple[Signal, ...]
    candidates: tuple[Candidate, ...]
    temporal_context: tuple[dict, ...] = ()
    traffic_observations: tuple[dict, ...] = ()
    observers: tuple[dict, ...] = ()
    topology: dict[str, tuple[str, ...]] = field(default_factory=dict)
    candidate_priors: dict[str, str] = field(default_factory=dict)
    evidence_owners: dict[str, str] = field(default_factory=dict)
    quality: dict = field(default_factory=dict)

    def sha256(self) -> str:
        fields = asdict(self)
        for name in ("temporal_context", "traffic_observations", "observers",
                     "topology", "candidate_priors", "evidence_owners", "quality"):
            if not fields.get(name):
                fields.pop(name, None)
        payload = json.dumps(fields, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Diagnosis:
    roots: tuple[str, ...]
    category: tuple[str, str]
    evidence_ids: tuple[str, ...]
    source: str = "rules"


def _city(node: str) -> str | None:
    if node.startswith("city:"):
        return node.split(":", 1)[1]
    if node.startswith("service-group:"):
        parts = node.split(":")
        return parts[1] if len(parts) >= 3 else None
    return node.split("-", 1)[0] if "-" in node else None


def _peer_prefix_context(store, event: Event, legal_nodes: frozenset[str]) -> dict[str, list[Signal]]:
    """Find route loss on still-established peers near a BGP incident."""
    if not hasattr(store, "iter_dimension_series") or not any(
        item.category == "bgp_session_down" and item.role == "direct" for item in event.signals
    ):
        return {}
    prefixes = []
    peer_up = {}
    for series in store.iter_dimension_series(source="routing"):
        if series.node_id not in legal_nodes:
            continue
        peer = dict(series.dimensions).get("peer")
        if not peer:
            continue
        key = (series.node_id, peer)
        if series.metric == "routing.bgp_peer_prefix_received":
            prefixes.append((key, series))
        elif series.metric == "routing.bgp_peer_up":
            peer_up[key] = series
    found: dict[str, list[Signal]] = {}
    for (node, peer), series in prefixes:
        status = peer_up.get((node, peer))
        if status is None:
            continue
        before = (series.time_indices >= max(0, event.start_minute - 30)) & (series.time_indices < event.start_minute)
        during = (series.time_indices >= event.start_minute) & (series.time_indices < event.end_minute)
        if before.sum() < 3 or during.sum() < 2:
            continue
        reference = float(np.median(series.values[before]))
        values = series.values[during]
        if reference < 2 or not np.any(values <= reference - 1):
            continue
        status_index = np.searchsorted(status.time_indices, series.time_indices[during])
        valid = status_index < len(status.time_indices)
        valid[valid] &= status.time_indices[status_index[valid]] == series.time_indices[during][valid]
        if (valid.sum() < 2 or np.any(status.values[status_index[valid]] < .5)
                or not np.any(valid & (values <= reference - 1))):
            continue
        minimum = int(np.argmin(np.where(valid, values, np.inf)))
        minute = int(series.time_indices[during][minimum])
        evidence_id = f"dimension:{minute}:{node}:bgp_peer_prefix_received:{peer}"
        found.setdefault(node, []).append(Signal(
            evidence_id, minute, node, "routing.bgp_peer_prefix_received", "context",
            float(values[minimum]), reference, 0, "auxiliary"))
    return found


def _temporal_context(store, event: Event, signals: list[Signal]) -> tuple[dict, ...]:
    if not hasattr(store, "node_mask"):
        return ()
    rows = []
    count = store.manifest["minute_count"]
    windows = {
        "before": (max(0, event.start_minute - 5), event.start_minute),
        "during": (event.start_minute, event.end_minute),
        "after": (event.end_minute, min(count, event.end_minute + 5)),
    }
    for signal in signals:
        parts = signal.evidence_id.split(":", 3)
        if len(parts) != 4 or parts[0] not in {"node", "edge", "log"} or not parts[2].isdigit():
            continue
        kind = parts[0]
        entity = int(parts[2])
        feature = store.feature_index(kind, signal.feature)
        values = getattr(store, f"{kind}_values")
        mask = getattr(store, f"{kind}_mask")
        if feature is None or entity >= values.shape[1]:
            continue
        row = {"evidence_id": signal.evidence_id}
        for label, (start, end) in windows.items():
            valid = mask[start:end, entity, feature]
            observed = values[start:end, entity, feature][valid]
            row[f"observed_{label}"] = int(len(observed))
            row[f"{label}_median"] = float(np.median(observed)) if len(observed) else None
        rows.append(row)
    return tuple(rows)


def _rule_candidates(contract, direct, prefix_context, primary_city, symptom_cities,
                     single_probe, probe_evidence) -> list[Candidate]:
    """Original rule-era candidate construction, kept unchanged for batch 1."""
    candidates: list[Candidate] = []
    for node in sorted(contract.nodes):
        if node in direct:
            evidence = direct[node]
            score = max(item.score for item in evidence) + .1 * (len(evidence) - 1)
            candidates.append(Candidate(node, score, tuple(sorted(item.evidence_id for item in evidence)),
                                        "direct_device_evidence"))
        elif node == single_probe:
            candidates.append(Candidate(node, .29, tuple(sorted(probe_evidence[node])),
                                        "single_probe_observer_possible_local_path_cause"))
        elif node in prefix_context:
            candidates.append(Candidate(node, .26, tuple(sorted(item.evidence_id for item in prefix_context[node])),
                                        "correlated_peer_prefix_change"))
        elif _city(node) == primary_city:
            score = (.30 if node.endswith(("service-vm-1", "service-vm-2", "service-vm-3"))
                     and primary_city in symptom_cities else .25)
            candidates.append(Candidate(node, score, (), "same_city_context_no_direct_evidence"))
        elif _city(node) in symptom_cities and node.endswith(("service-vm-1", "service-vm-2", "service-vm-3")):
            candidates.append(Candidate(node, .15, (), "symptom_target_city_no_verified_instance_mapping"))
    candidates.sort(key=lambda item: (-item.score, item.node))
    candidates = candidates[:12]
    if len(candidates) < 8:
        seen = {item.node for item in candidates}
        for node in sorted(contract.nodes):
            if node not in seen:
                candidates.append(Candidate(node, 0, (), "coverage_fallback_no_direct_evidence"))
                if len(candidates) >= 8:
                    break
    return candidates


_CITY_ROLE = {"br-1", "br-2", "cr-1", "cr-2", "fw", "traffic-vm",
              "service-vm-1", "service-vm-2", "service-vm-3", "monitor-vm"}


def _service_node_for(store, signal: Signal, contract: OfficialContract) -> str | None:
    node = signal.node
    if node in contract.nodes:
        return node
    if node.startswith("service-group:"):
        parts = node.split(":")
        if len(parts) >= 3 and f"{parts[1]}-service-vm-1" in contract.nodes:
            return f"{parts[1]}-service-vm-1"
    return None


def _netflow_candidates(store, signals: list[Signal], contract: OfficialContract
                        ) -> tuple[list[Candidate], dict[str, str]]:
    """Candidate set driven by service/path NetFlow evidence."""
    priors: dict[str, str] = {}
    candidates: list[Candidate] = []

    def add(node: str, score: float, reason: str, evidence=()):
        if node not in contract.nodes or node in priors:
            return
        priors[node] = reason
        candidates.append(Candidate(node, score, tuple(evidence), reason))

    netflow = [signal for signal in signals if signal.feature.startswith("netflow.")]
    direct_netflow = [signal for signal in netflow if signal.role == "direct"]
    context = [signal for signal in netflow if signal.role == "context"]
    symptom_netflow = [signal for signal in netflow if signal.role == "symptom"]
    if direct_netflow:
        target = direct_netflow[0].node
        city = target.split("-", 1)[0]
        target_role = target.split("-", 1)[1] if "-" in target else ""
        if target_role.startswith(("br", "cr", "fw")):
            add(target, 0.95, "path_target", (direct_netflow[0].evidence_id,))
            for role, score in (("br-2", 0.80), ("cr-1", 0.80), ("cr-2", 0.75), ("fw", 0.75)):
                add(f"{city}-{role}", score, "same_city_path_fallback")
        else:
            add(target, 0.95, "service_target", (direct_netflow[0].evidence_id,))
            for role in ("service-vm-2", "service-vm-3"):
                add(f"{city}-{role}", 0.60, "same_city_service")
            for role in ("fw", "cr-1", "br-1"):
                add(f"{city}-{role}", 0.45, "same_city_fallback")
    else:
        for signal in context:
            add(signal.node, 0.90 if signal.node.endswith("-br-1") else 0.80,
                "path_observer_city_device", (signal.evidence_id,))
        for signal in symptom_netflow:
            add(signal.node, 0.50, "service_target_under_observer_failure", (signal.evidence_id,))
        for city in sorted({signal.node.split("-", 1)[0] for signal in context}):
            for role in ("cr-1", "br-2", "fw"):
                add(f"{city}-{role}", 0.75 if role == "cr-1" else 0.70,
                    "path_observer_city_device")
    candidates.sort(key=lambda item: (-item.score, item.node))
    candidates = candidates[:12]
    if len(candidates) < 8:
        seen = {item.node for item in candidates}
        for node in sorted(contract.nodes):
            if node not in seen:
                candidates.append(Candidate(node, 0, (), "coverage_fallback_no_direct_evidence"))
                if len(candidates) >= 8:
                    break
    return candidates, priors


def _observers_from_event(store, event: Event, signals: list[Signal]) -> tuple[dict, ...]:
    rows: list[dict] = list(event.observers)
    if not hasattr(store, "edges"):
        return tuple(rows)
    for signal in signals:
        if not signal.evidence_id.startswith("edge:") or not signal.feature.startswith("traffic."):
            continue
        parts = signal.evidence_id.split(":", 3)
        if len(parts) != 4 or not parts[2].isdigit():
            continue
        index = int(parts[2])
        if not 0 <= index < len(store.edges):
            continue
        edge = store.edges[index]
        if edge.get("relation") != "traffic":
            continue
        source = str(edge.get("source", ""))
        rows.append({
            "kind": "traffic",
            "source_city": source.split("-", 1)[0] if "-" in source else source,
            "observer": source,
            "target": edge.get("target"),
            "feature": signal.feature,
            "value": round(float(signal.value), 6),
            "stance": "support",
        })
    return tuple(rows)


def _evidence_owners(signals: list[Signal], contract: OfficialContract) -> dict[str, str]:
    owners: dict[str, str] = {}
    for signal in signals:
        owner = signal.node
        if owner not in contract.nodes and owner.startswith("service-group:"):
            parts = owner.split(":")
            if len(parts) >= 3:
                candidate = f"{parts[1]}-service-vm-1"
                if candidate in contract.nodes:
                    owner = candidate
        owners[signal.evidence_id] = owner
    return owners


def build_evidence(
    store, event: Event, contract: OfficialContract, *, include_probe_candidates: bool = False,
    include_routing_context: bool = False, include_temporal_context: bool = False,
) -> EvidencePack:
    signals = list(event.signals)
    direct: dict[str, list[Signal]] = {}
    symptom_cities: set[str] = set()
    probe_evidence: dict[str, set[str]] = {}
    traffic_observations: dict[str, dict] = {}
    for signal in event.signals:
        if signal.role == "direct" and signal.node in contract.nodes:
            direct.setdefault(signal.node, []).append(signal)
        elif signal.role == "symptom":
            city = _city(signal.node)
            if city:
                symptom_cities.add(city)
            if signal.evidence_id.startswith("edge:") and hasattr(store, "edges"):
                parts = signal.evidence_id.split(":", 3)
                if len(parts) == 4 and parts[2].isdigit():
                    index = int(parts[2])
                    if 0 <= index < len(store.edges):
                        edge = store.edges[index]
                        if edge.get("relation") == "traffic" and edge.get("target") == signal.node:
                            probe_evidence.setdefault(edge["source"], set()).add(signal.evidence_id)
                            traffic_observations[signal.evidence_id] = {
                                "evidence_id": signal.evidence_id,
                                "observer": edge["source"],
                                "target": edge["target"],
                            }
    prefix_context = (_peer_prefix_context(store, event, contract.nodes)
                      if include_routing_context else {})
    for context_signals in prefix_context.values():
        signals.extend(context_signals)
    if hasattr(store, "node_mask"):
        node_index = {name: i for i, name in enumerate(store.nodes)}
        for node in sorted(direct, key=lambda item: -max(signal.score for signal in direct[item]))[:5]:
            index = node_index.get(node)
            if index is None:
                continue
            for kind, names in (
                ("node", ("netflow.bytes", "scrape.scrape_up")),
                ("log", tuple(name for name in store.manifest["features"]["log"] if name.startswith("frr.") and ".err." in name)),
            ):
                for name in names:
                    column = store.feature_index(kind, name)
                    if column is None:
                        continue
                    mask = getattr(store, f"{kind}_mask")[event.start_minute:event.end_minute, index, column]
                    if not np.any(mask):
                        continue
                    values = getattr(store, f"{kind}_values")[event.start_minute:event.end_minute, index, column]
                    observed = np.flatnonzero(mask)
                    offset = int(observed[np.argmin(values[observed]) if name == "scrape.scrape_up" else np.argmax(values[observed])])
                    value = float(values[offset])
                    if kind == "log" and value <= 0:
                        continue
                    if name == "scrape.scrape_up" and value != 0:
                        continue
                    minute = event.start_minute + offset
                    role = "quality" if name == "scrape.scrape_up" else "auxiliary"
                    signals.append(Signal(f"{kind}:{minute}:{index}:{name}", minute, node, name, "context", value, 0, 0, role))
            netflow_features = [name for name in store.manifest["features"]["edge"] if name.startswith("netflow.bytes.protocol_")]
            edge_indexes = [i for i, edge in enumerate(store.edges) if edge.get("relation") == "netflow" and edge.get("source") == node]
            additions = 0
            for edge_index in edge_indexes:
                for name in netflow_features:
                    column = store.feature_index("edge", name)
                    mask = store.edge_mask[event.start_minute:event.end_minute, edge_index, column]
                    if not np.any(mask):
                        continue
                    values = store.edge_values[event.start_minute:event.end_minute, edge_index, column]
                    offset = int(np.argmax(np.where(mask, values, -np.inf)))
                    minute = event.start_minute + offset
                    signals.append(Signal(f"edge:{minute}:{edge_index}:{name}", minute, node, name, "context", float(values[offset]), 0, 0, "auxiliary"))
                    additions += 1
                    if additions >= 3:
                        break
                if additions >= 3:
                    break
    leader = max(direct, key=lambda node: max(item.score for item in direct[node])) if direct else None
    primary_city = _city(leader) if leader else (sorted(symptom_cities)[0] if symptom_cities else None)
    single_probe = (next(iter(probe_evidence)) if include_probe_candidates
                    and not direct and len(probe_evidence) == 1 else None)
    candidate_priors: dict[str, str] = {}
    if any(signal.feature.startswith("netflow.") for signal in signals):
        candidates, candidate_priors = _netflow_candidates(store, list(signals), contract)
        seen = {candidate.node for candidate in candidates}
        for node in sorted(direct):
            if node in seen:
                continue
            evidence = direct[node]
            score = max(item.score for item in evidence) + .1 * (len(evidence) - 1)
            candidates.append(Candidate(node, score, tuple(sorted(item.evidence_id for item in evidence)),
                                        "direct_device_evidence"))
            candidate_priors.setdefault(node, "direct_device_evidence")
        candidates.sort(key=lambda item: (-item.score, item.node))
        candidates = candidates[:12]
        if len(candidates) < 8:
            seen = {candidate.node for candidate in candidates}
            for node in sorted(contract.nodes):
                if node not in seen:
                    candidates.append(Candidate(node, 0, (), "coverage_fallback_no_direct_evidence"))
                    if len(candidates) >= 8:
                        break
    else:
        candidates = _rule_candidates(contract, direct, prefix_context, primary_city,
                                      symptom_cities, single_probe, probe_evidence)
        for candidate in candidates:
            candidate_priors.setdefault(candidate.node, candidate.reason)
    topology = {}
    for candidate in candidates:
        parts = candidate.node.split("-", 1)
        if len(parts) == 2:
            topology[candidate.node] = (f"city:{parts[0]}", f"role:{parts[1]}")
    observers = _observers_from_event(store, event, signals)
    evidence_owners = _evidence_owners(signals, contract)
    quality = {
        "has_direct": bool(direct),
        "netflow_observers": sum(1 for row in observers if row.get("kind") == "netflow"),
        "traffic_observers": sum(1 for row in observers if row.get("kind") == "traffic"),
        "signal_count": len(signals),
        "weak_evidence": not direct,
    }
    return EvidencePack(
        event.event_id,
        store.time_at(event.start_minute).isoformat().replace("+00:00", "Z"),
        store.time_at(event.end_minute).isoformat().replace("+00:00", "Z"),
        tuple(signals),
        tuple(candidates),
        _temporal_context(store, event, signals) if include_temporal_context else (),
        tuple(traffic_observations[key] for key in sorted(traffic_observations)),
        observers,
        topology,
        candidate_priors,
        evidence_owners,
        quality,
    )


def _category(signals: list[Signal], root: str, contract: OfficialContract) -> tuple[str, str]:
    scores: dict[str, float] = {}
    for item in signals:
        scores[item.category] = max(scores.get(item.category, 0), item.score)
    specific = [
        (label, scores[label])
        for label, threshold in (("disk_io_pressure", 5), ("memory_pressure", 4), ("disk_space_low", 4))
        if scores.get(label, 0) >= threshold and ("resource", label) in contract.categories
    ]
    if specific:
        return ("resource", max(specific, key=lambda item: item[1])[0])
    ordered = sorted(signals, key=lambda item: -item.score)
    for signal in ordered:
        mapping = {
            "cpu_pressure": ("firewall" if root.endswith("-fw") else "resource", "cpu_pressure"),
            "process_pressure": ("resource", "process_pressure"),
            "link_error": ("link", "loss"),
            "link_loss": ("link", "loss"),
            "link_down": ("link", "loss"),
            "bgp_session_down": ("routing", "bgp_session_down"),
            "bgp_route_filter": ("routing", "bgp_route_filter"),
            "blackhole": ("routing", "blackhole"),
            "ospf6_neighbor_down": ("routing", "ospf6_neighbor_down"),
            "ospf6_interface_flap": ("routing", "ospf6_interface_flap"),
            "ospf6_cost_anomaly": ("routing", "ospf6_cost_anomaly"),
            "bgp_route_flap": ("routing", "bgp_route_flap"),
        }
        pair = mapping.get(signal.category)
        if pair in contract.categories:
            return pair
    for signal in ordered:
        feature = signal.feature
        if feature == "netflow.dns.response_ratio":
            pair = ("service", "dns_down")
        elif feature == "netflow.web.response_ratio":
            pair = ("service", "web_5xx")
        elif feature == "netflow.auth.response_ratio":
            pair = ("service", "auth_timeout")
        elif feature == "netflow.path.response_ratio":
            pair = ("link", "loss")
        elif ".request_volume_ratio" in feature:
            pair = ("link", "loss")
        elif "traffic.dns." in feature:
            pair = ("service", "dns_down")
        elif "traffic.web." in feature:
            pair = ("service", "web_slow" if "latency" in feature else "web_5xx")
        elif "traffic.auth." in feature:
            pair = ("service", "auth_timeout" if "latency" in feature else "auth_error")
        elif feature == "traffic.elephant.loss_rate":
            pair = ("link", "loss")
        else:
            continue
        if pair in contract.categories:
            return pair
    return ("resource", "cpu_pressure")


def diagnose_rules(pack: EvidencePack, contract: OfficialContract) -> Diagnosis:
    roots = tuple(candidate.node for candidate in pack.candidates[:5])
    if len(roots) != 5 or any(root not in contract.nodes for root in roots):
        raise ValueError("candidate set cannot produce official Top-5")
    top = roots[0]
    direct = [item for item in pack.signals if item.node == top and item.role == "direct"]
    symptoms = [item for item in pack.signals if item.role == "symptom"]
    category = _category(direct if direct else symptoms, top, contract)
    evidence_ids = tuple(sorted({item.evidence_id for item in direct + symptoms[:3]}))
    return Diagnosis(roots, category, evidence_ids)

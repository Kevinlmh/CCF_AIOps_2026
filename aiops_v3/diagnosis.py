"""Evidence-bound candidate retrieval and deterministic diagnosis."""

from __future__ import annotations

from dataclasses import asdict, dataclass
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

    def sha256(self) -> str:
        payload = json.dumps(asdict(self), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
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


def build_evidence(store, event: Event, contract: OfficialContract) -> EvidencePack:
    signals = list(event.signals)
    direct: dict[str, list[Signal]] = {}
    symptom_cities: set[str] = set()
    for signal in event.signals:
        if signal.role == "direct" and signal.node in contract.nodes:
            direct.setdefault(signal.node, []).append(signal)
        elif signal.role == "symptom":
            city = _city(signal.node)
            if city:
                symptom_cities.add(city)
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
    candidates: list[Candidate] = []
    for node in sorted(contract.nodes):
        if node in direct:
            evidence = direct[node]
            score = max(item.score for item in evidence) + .1 * (len(evidence) - 1)
            candidates.append(Candidate(node, score, tuple(sorted(item.evidence_id for item in evidence)), "direct_device_evidence"))
        elif _city(node) == primary_city:
            score = .30 if node.endswith(("service-vm-1", "service-vm-2", "service-vm-3")) and primary_city in symptom_cities else .25
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
    return EvidencePack(
        event.event_id,
        store.time_at(event.start_minute).isoformat().replace("+00:00", "Z"),
        store.time_at(event.end_minute).isoformat().replace("+00:00", "Z"),
        tuple(signals),
        tuple(candidates),
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
            "blackhole": ("routing", "blackhole"),
            "ospf6_neighbor_down": ("routing", "ospf6_neighbor_down"),
        }
        pair = mapping.get(signal.category)
        if pair in contract.categories:
            return pair
    for signal in ordered:
        feature = signal.feature
        if "traffic.dns." in feature:
            pair = ("service", "dns_down")
        elif "traffic.web." in feature:
            pair = ("service", "web_slow" if "latency" in feature else "web_5xx")
        elif "traffic.auth." in feature:
            pair = ("service", "auth_timeout" if "latency" in feature else "auth_error")
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

"""Masked change detection with explicit direct and symptom evidence."""

from __future__ import annotations

from dataclasses import dataclass
import warnings

import numpy as np

from .store import FeatureStore


@dataclass(frozen=True)
class DetectorSettings:
    score_threshold: float = 4.0
    merge_gap_minutes: int = 1
    max_event_minutes: int = 30
    max_signals_per_event: int = 60


@dataclass(frozen=True)
class Signal:
    evidence_id: str
    minute: int
    node: str
    feature: str
    category: str
    value: float
    reference: float
    score: float
    role: str  # direct or symptom


@dataclass(frozen=True)
class Event:
    event_id: str
    start_minute: int
    end_minute: int  # exclusive
    signals: tuple[Signal, ...]


@dataclass(frozen=True)
class DetectionResult:
    events: tuple[Event, ...]
    rejected: tuple[dict, ...]


@dataclass(frozen=True)
class _Rule:
    feature: str
    category: str
    direction: int
    floor: float
    absolute: float
    immediate: bool = False


_NODE_RULES = (
    _Rule("node.cpu_usage", "cpu_pressure", 1, 4.0, 18.0),
    _Rule("node.memory_available_ratio", "memory_pressure", -1, .015, .90),
    _Rule("node.disk_io_util", "disk_io_pressure", 1, 10.0, 55.0),
    _Rule("node.filesystem_used_ratio", "disk_space_low", 1, .025, .80),
    _Rule("node.process_count", "process_pressure", 1, 10.0, 100.0),
    _Rule("interface.rx_error_rate", "link_error", 1, .1, .1),
    _Rule("interface.tx_error_rate", "link_error", 1, .1, .1),
    _Rule("interface.rx_drop_rate", "link_loss", 1, .1, .1),
    _Rule("interface.tx_drop_rate", "link_loss", 1, .1, .1),
    _Rule("interface.carrier_changes", "link_down", 1, .5, 1.0, True),
    _Rule("routing.bgp_peer_up", "bgp_session_down", -1, .2, .5, True),
    _Rule("routing.bgp_command_success", "bgp_session_down", -1, .2, .5, True),
    _Rule("routing.ipv6_route_exists", "blackhole", -1, .2, .5, True),
    _Rule("routing.ospf6_neighbor_state_code", "ospf6_neighbor_down", -1, .5, 3.0, True),
)


def _scores(data: np.ndarray, mask: np.ndarray, rule: _Rule) -> tuple[np.ndarray, np.ndarray]:
    clean = np.where(mask, data, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        center = np.nanmedian(clean, axis=0)
        mad = np.nanmedian(np.abs(clean - center), axis=0)
    enough = mask.sum(axis=0) >= 8
    scale = np.maximum(np.nan_to_num(1.4826 * mad, nan=0), rule.floor)
    deviation = rule.direction * (data - center[None, :])
    score = np.where(mask & enough[None, :] & np.isfinite(data), deviation / scale[None, :], 0)
    if rule.direction == 1:
        score = np.where(data >= rule.absolute, score, 0)
    else:
        score = np.where(data <= rule.absolute, score, 0)
    return np.maximum(score, 0).astype(np.float32), np.nan_to_num(center, nan=0)


def _supported(score: np.ndarray, threshold: float, immediate: bool) -> np.ndarray:
    active = score >= threshold
    if immediate:
        return active
    previous = np.zeros_like(active)
    following = np.zeros_like(active)
    previous[1:] = active[:-1]
    following[:-1] = active[1:]
    return active & (previous | following)


def detect_with_audit(store: FeatureStore, settings: DetectorSettings = DetectorSettings()) -> DetectionResult:
    count = store.manifest["minute_count"]
    global_active = np.zeros(count, bool)
    scored: list[tuple[_Rule, np.ndarray, np.ndarray, str]] = []
    for rule in _NODE_RULES:
        column = store.feature_index("node", rule.feature)
        if column is None:
            continue
        data = np.asarray(store.node_values[:, :, column], np.float32)
        mask = np.asarray(store.node_mask[:, :, column], bool)
        score, center = _scores(data, mask, rule)
        active = _supported(score, settings.score_threshold, rule.immediate)
        score = np.where(active, score, 0)
        global_active |= active.any(axis=1)
        scored.append((rule, score, center, "node"))
    for name in store.manifest["features"]["edge"]:
        if not name.startswith("traffic.") or not name.endswith((".error_ratio", ".loss_rate", ".latency_p95_seconds")):
            continue
        column = store.feature_index("edge", name)
        data = np.asarray(store.edge_values[:, :, column], np.float32)
        mask = np.asarray(store.edge_mask[:, :, column], bool)
        absolute = .15 if name.endswith("error_ratio") else .05 if name.endswith("loss_rate") else .15
        rule = _Rule(name, name.split(".")[1], 1, .03 if name.endswith("ratio") else .05, absolute)
        score, center = _scores(data, mask, rule)
        active = _supported(score, settings.score_threshold, False)
        score = np.where(active, score, 0)
        global_active |= active.any(axis=1)
        scored.append((rule, score, center, "edge"))

    positions = np.flatnonzero(global_active)
    if not len(positions):
        return DetectionResult((), ())
    groups: list[tuple[int, int]] = []
    begin = previous = int(positions[0])
    for position in positions[1:]:
        current = int(position)
        if current - previous > settings.merge_gap_minutes + 1:
            groups.append((begin, previous + 1))
            begin = current
        previous = current
    groups.append((begin, previous + 1))

    events: list[Event] = []
    rejected: list[dict] = []
    for start, end in groups:
        if end - start > settings.max_event_minutes:
            rejected.append({"start_minute": start, "end_minute": end, "reason": "duration_over_30_minutes"})
            continue
        signals: list[Signal] = []
        for rule, score, center, kind in scored:
            window = score[start:end]
            if not np.any(window):
                continue
            for entity in np.flatnonzero(np.max(window, axis=0) > 0):
                offset = int(np.argmax(window[:, entity]))
                minute = start + offset
                value = float((store.node_values if kind == "node" else store.edge_values)[minute, entity, store.feature_index(kind, rule.feature)])
                node = store.nodes[int(entity)] if kind == "node" else store.edges[int(entity)]["target"]
                signals.append(Signal(
                    f"{kind}:{minute}:{int(entity)}:{rule.feature}", minute, node,
                    rule.feature, rule.category, value, float(center[entity]),
                    float(window[offset, entity]), "direct" if kind == "node" else "symptom",
                ))
        signals.sort(key=lambda item: (-item.score, item.evidence_id))
        if not signals:
            rejected.append({"start_minute": start, "end_minute": end, "reason": "no_valid_signal"})
            continue
        events.append(Event(f"v3-e{len(events) + 1:06d}", start, end, tuple(signals[:settings.max_signals_per_event])))
    return DetectionResult(tuple(events), tuple(rejected))


def detect(store: FeatureStore, settings: DetectorSettings = DetectorSettings()) -> list[Event]:
    return list(detect_with_audit(store, settings).events)

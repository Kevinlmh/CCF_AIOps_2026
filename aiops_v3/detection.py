"""Masked change detection with explicit direct and symptom evidence."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import warnings

import numpy as np

from .store import FeatureStore


@dataclass(frozen=True)
class DetectorSettings:
    score_threshold: float = 4.0
    merge_gap_minutes: int = 1
    max_event_minutes: int = 30
    max_signals_per_event: int = 60
    bgp_flap_gap_minutes: int = 4
    coincident_onset_minutes: int = 1
    coincident_overlap_fraction: float = .5
    trajectory_correlation_min: float = .75
    min_component_dice: float = .4
    weak_cpu_max_minutes: int = 0
    weak_cpu_max_score: float = 0.0
    request_aware_service: bool = False
    service_overlap_policy: str = "city"
    baseline_strategy: str = "global_median"
    routing_dimension_detection: bool = False
    cross_city_merge_policy: str = "correlated"
    rule_overrides: dict[str, dict[str, float]] = field(default_factory=dict)


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
    merged: tuple[dict, ...] = ()


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
    _Rule("routing.ospf6_interface_cost", "ospf6_cost_anomaly", 1, 10.0, 50.0),
)


def _scores(data: np.ndarray, mask: np.ndarray, rule: _Rule,
            baseline_strategy: str = "global_median") -> tuple[np.ndarray, np.ndarray]:
    clean = np.where(mask, data, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        center = np.nanmedian(clean, axis=0)
        mad = np.nanmedian(np.abs(clean - center), axis=0)
        observed_count = mask.sum(axis=0)
        # Public cases contain short excerpts around a fault. When the fault
        # occupies most observed samples, the median is the fault level.
        # Use the healthy-side tail only for sparse series; long series retain
        # the more stable median baseline.
        sparse = (observed_count >= 8) & (observed_count < 60)
        eligible = observed_count >= 8 if baseline_strategy == "healthy_tail" else sparse
        for column in np.flatnonzero(eligible):
            center[column] = np.nanpercentile(clean[:, column], 20 if rule.direction == 1 else 80)
            mad[column] = np.nanpercentile(np.abs(clean[:, column] - center[column]), 20)
    enough = observed_count >= 8
    scale = np.maximum(np.nan_to_num(1.4826 * mad, nan=0), rule.floor)
    if baseline_strategy == "rolling_healthy_tail":
        # A block only sees observations strictly before its first minute.
        # Until eight prior samples exist, the block cannot trigger a signal.
        rolling_center = np.empty_like(data)
        rolling_scale = np.empty_like(data)
        rolling_enough = np.empty_like(mask)
        for start in range(0, len(data), 30):
            end = min(len(data), start + 30)
            history = clean[max(0, start - 1440):start]
            history_count = np.isfinite(history).sum(axis=0)
            if len(history):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    prior_center = np.nanpercentile(
                        history, 20 if rule.direction == 1 else 80, axis=0)
                    prior_mad = np.nanpercentile(np.abs(history - prior_center), 20, axis=0)
                block_center = np.where(history_count >= 8, prior_center, center)
                block_scale = np.where(
                    history_count >= 8,
                    np.maximum(np.nan_to_num(1.4826 * prior_mad, nan=0), rule.floor),
                    scale,
                )
            else:
                block_center, block_scale = center, scale
            rolling_center[start:end] = block_center
            rolling_scale[start:end] = block_scale
            rolling_enough[start:end] = history_count >= 8
        deviation = rule.direction * (data - rolling_center)
        score = np.where(mask & rolling_enough & np.isfinite(data),
                         deviation / rolling_scale, 0)
        center = rolling_center
    else:
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


def _groups(active: np.ndarray, settings: DetectorSettings) -> list[tuple[int, int]]:
    positions = np.flatnonzero(active)
    if not len(positions):
        return []
    spans: list[tuple[int, int]] = []
    begin = previous = int(positions[0])
    for position in positions[1:]:
        current = int(position)
        if current - previous > settings.merge_gap_minutes + 1:
            spans.append((begin, previous + 1))
            begin = current
        previous = current
    spans.append((begin, previous + 1))
    return [
        (part, min(end, part + settings.max_event_minutes))
        for start, end in spans
        for part in range(start, end, settings.max_event_minutes)
    ]


def _target_city(target: str) -> str | None:
    if target.startswith("city:"):
        return target.split(":", 1)[1]
    if target.startswith("service-group:"):
        return target.split(":")[1]
    return target.split("-", 1)[0] if "-" in target else None


def _routing_dimension_events(store: FeatureStore, settings: DetectorSettings) -> list[Event]:
    """Require peer prefix loss and independent route loss on a stable session."""
    count = store.manifest["minute_count"]
    prefixes = {}
    peer_up = {}
    route_counts = {}
    route_changes: dict[str, list] = {}
    ospf_enabled = {}
    ospf_neighbors: dict[tuple[str, str], list] = {}
    for series in store.iter_dimension_series(source="routing"):
        dimensions = dict(series.dimensions)
        if series.metric == "routing.bgp_peer_prefix_received" and "peer" in dimensions:
            prefixes[(series.node_id, dimensions["peer"])] = series
        elif series.metric == "routing.bgp_peer_up" and "peer" in dimensions:
            peer_up[(series.node_id, dimensions["peer"])] = series
        elif (series.metric == "routing.ipv6_route_count"
              and dimensions.get("protocol") == "all"):
            route_counts[series.node_id] = series
        elif series.metric == "routing.ipv6_route_change_total":
            route_changes.setdefault(series.node_id, []).append(series)
        elif (series.metric == "routing.ospf6_interface_enabled"
              and "interface" in dimensions):
            ospf_enabled[(series.node_id, dimensions["interface"])] = series
        elif (series.metric == "routing.ospf6_neighbor_state_code"
              and "interface" in dimensions):
            ospf_neighbors.setdefault((series.node_id, dimensions["interface"]), []).append(series)

    def dense(series):
        values = np.full(count, np.nan, np.float32)
        values[series.time_indices] = series.values
        return values

    grouped_signals: dict[tuple[str, str, int, int], dict[str, Signal]] = {}
    for (node, peer), prefix_series in prefixes.items():
        if (node, peer) not in peer_up or (node not in route_counts and node not in route_changes):
            continue
        prefix = dense(prefix_series)
        routes = dense(route_counts[node]) if node in route_counts else None
        status = dense(peer_up[(node, peer)])
        if np.isfinite(prefix).sum() < 8:
            continue
        prefix_reference = float(np.nanpercentile(prefix, 80))
        route_reference = (float(np.nanpercentile(routes, 80))
                           if routes is not None and np.isfinite(routes).sum() >= 8 else 0)
        if prefix_reference < 2:
            continue
        active = (np.isfinite(prefix) & np.isfinite(status) & (status >= .5)
                  & (prefix <= prefix_reference - 1)
                  & (prefix <= prefix_reference * .75))
        active = _supported(active.astype(np.float32), 1, False)
        for start, end in _groups(active, settings):
            route_drop = (routes is not None and route_reference >= 2
                          and np.any(np.isfinite(routes[start:end])
                                     & (routes[start:end] <= route_reference - 1)))
            changed = []
            for route_change in route_changes.get(node, []):
                values = dense(route_change)
                delta = values[1:] - values[:-1]
                first_change = max(1, start)
                window = delta[first_change - 1:end - 1]
                positions = np.flatnonzero(np.isfinite(window) & (window > 0)) + first_change
                if len(positions):
                    minute = int(positions[0])
                    changed.append((route_change, minute, float(values[minute]),
                                    float(values[minute - 1])))
            if not route_drop and not changed:
                continue
            prefix_minute = start + int(np.nanargmin(prefix[start:end]))
            score = max(4.0, 8 * (1 - float(prefix[prefix_minute]) / prefix_reference))
            signals = [
                Signal(f"dimension:{prefix_minute}:{node}:bgp_peer_prefix_received:{peer}",
                       prefix_minute, node, "routing.bgp_peer_prefix_received",
                       "bgp_route_filter", float(prefix[prefix_minute]), prefix_reference,
                       score, "direct"),
            ]
            if route_drop:
                route_minute = start + int(np.nanargmin(routes[start:end]))
                signals.append(Signal(f"dimension:{route_minute}:{node}:ipv6_route_count:all",
                       route_minute, node, "routing.ipv6_route_count",
                       "bgp_route_filter", float(routes[route_minute]), route_reference,
                       score, "direct"))
            for route_change, minute, value, reference in changed[:2]:
                prefix_label = dict(route_change.dimensions).get("prefix", "unknown")
                signals.append(Signal(f"dimension:{minute}:{node}:ipv6_route_change_total:{prefix_label}",
                                      minute, node, "routing.ipv6_route_change_total",
                                      "bgp_route_filter", value, reference, score, "direct"))
            group = grouped_signals.setdefault(("bgp_route_filter", node, start, end), {})
            group.update((signal.evidence_id, signal) for signal in signals)
    for (node, interface), series in ospf_enabled.items():
        enabled = dense(series)
        active = _supported((enabled == 0).astype(np.float32), 1, False)
        for start, end in _groups(active, settings):
            if start == 0 or end >= count or not (enabled[start - 1] >= .5 and enabled[end] >= .5):
                continue
            neighbor_matches = []
            for neighbor in ospf_neighbors.get((node, interface), []):
                state = dense(neighbor)
                if (np.isfinite(state[start:end]).sum() >= 2
                        and state[start - 1] >= 4 and state[end] >= 4
                        and np.nanmin(state[start:end]) <= 3):
                    neighbor_matches.append((neighbor, state))
            if not neighbor_matches:
                continue
            neighbor, state = neighbor_matches[0]
            minute = start + int(np.nanargmin(state[start:end]))
            evidence = (
                Signal(f"dimension:{start}:{node}:ospf6_interface_enabled:{interface}",
                       start, node, "routing.ospf6_interface_enabled", "ospf6_interface_flap",
                       0, 1, 5, "direct"),
                Signal(f"dimension:{minute}:{node}:ospf6_neighbor_state_code:{interface}",
                       minute, node, "routing.ospf6_neighbor_state_code", "ospf6_interface_flap",
                       float(state[minute]), 6, 5, "direct"),
            )
            group = grouped_signals.setdefault(("ospf6_interface_flap", node, start, end), {})
            group.update((signal.evidence_id, signal) for signal in evidence)
    return [Event(f"v3-dim{index:06d}", start, end,
                  tuple(group.values())[:settings.max_signals_per_event])
            for index, ((_, _, start, end), group) in enumerate(sorted(grouped_signals.items()), 1)]


def _direct_nodes(event: Event) -> set[str]:
    return {signal.node for signal in event.signals if signal.role == "direct"}


def _dominant_direct_category(event: Event) -> str | None:
    direct = [signal for signal in event.signals if signal.role == "direct"]
    return max(direct, key=lambda signal: signal.score).category if direct else None


def _joined(left: Event, right: Event, settings: DetectorSettings) -> Event:
    signals = {signal.evidence_id: signal for signal in (*left.signals, *right.signals)}
    ordered = sorted(signals.values(), key=lambda signal: (-signal.score, signal.evidence_id))
    return Event(left.event_id, min(left.start_minute, right.start_minute),
                 max(left.end_minute, right.end_minute), tuple(ordered[:settings.max_signals_per_event]))


def _trajectory_agrees(store: FeatureStore, left: Event, right: Event, settings: DetectorSettings) -> bool:
    start = max(0, min(left.start_minute, right.start_minute) - 3)
    end = min(store.manifest["minute_count"], max(left.end_minute, right.end_minute) + 3)
    node_index = {node: index for index, node in enumerate(store.nodes)}
    correlations: list[float] = []
    for first in left.signals:
        if first.role != "direct":
            continue
        for second in right.signals:
            if second.role != "direct" or first.feature != second.feature or first.node == second.node:
                continue
            column = store.feature_index("node", first.feature)
            if column is None:
                continue
            a, b = node_index[first.node], node_index[second.node]
            observed = store.node_mask[start:end, a, column] & store.node_mask[start:end, b, column]
            if observed.sum() < 5:
                continue
            x = np.asarray(store.node_values[start:end, a, column][observed], np.float64)
            y = np.asarray(store.node_values[start:end, b, column][observed], np.float64)
            if np.std(x) <= 1e-6 or np.std(y) <= 1e-6:
                continue
            correlations.append(float(np.corrcoef(x, y)[0, 1]))
    return bool(correlations and max(correlations) >= settings.trajectory_correlation_min
                and np.median(correlations) >= settings.trajectory_correlation_min - .1)


def _consolidate(store: FeatureStore, events: list[Event], settings: DetectorSettings) -> tuple[list[Event], list[dict]]:
    merged: list[dict] = []
    components = {event.event_id: [(event.start_minute, event.end_minute)] for event in events}
    lineage = {event.event_id: [event.event_id] for event in events}

    def keeps_matchable_windows(left: Event, right: Event) -> bool:
        start = min(left.start_minute, right.start_minute)
        end = max(left.end_minute, right.end_minute)
        return all(
            2 * (min(component_end, end) - max(component_start, start))
            / (component_end - component_start + end - start) >= settings.min_component_dice
            for component_start, component_end in (*components[left.event_id], *components[right.event_id])
        )

    stitched: list[Event] = []
    for event in events:
        target = next((previous for previous in reversed(stitched)
                       if _dominant_direct_category(previous) == _dominant_direct_category(event) == "bgp_session_down"
                       and len(_direct_nodes(previous)) == len(_direct_nodes(event)) == 1
                       and _direct_nodes(previous) == _direct_nodes(event)
                       and 0 <= event.start_minute - previous.end_minute <= settings.bgp_flap_gap_minutes
                       and event.end_minute - previous.start_minute <= settings.max_event_minutes
                       and keeps_matchable_windows(previous, event)), None)
        if target is None:
            stitched.append(event)
        else:
            stitched[stitched.index(target)] = _joined(target, event, settings)
            components[target.event_id].extend(components[event.event_id])
            lineage[target.event_id].extend(lineage[event.event_id])
            merged.append({"reason": "same_node_bgp_flap", "source_event_ids": [target.event_id, event.event_id]})
    consolidated: list[Event] = []
    for event in stitched:
        category = _dominant_direct_category(event)
        target = None
        if category:
            for previous in reversed(consolidated):
                if event.start_minute - previous.start_minute > settings.coincident_onset_minutes:
                    break
                if _dominant_direct_category(previous) != category or _direct_nodes(previous) & _direct_nodes(event):
                    continue
                if (settings.cross_city_merge_policy == "same_city"
                        and len({_target_city(node) for node in
                                 (_direct_nodes(previous) | _direct_nodes(event))}) != 1):
                    continue
                overlap = min(previous.end_minute, event.end_minute) - max(previous.start_minute, event.start_minute)
                shorter = min(previous.end_minute - previous.start_minute, event.end_minute - event.start_minute)
                span = max(previous.end_minute, event.end_minute) - min(previous.start_minute, event.start_minute)
                if (overlap >= shorter * settings.coincident_overlap_fraction
                        and span <= settings.max_event_minutes
                        and keeps_matchable_windows(previous, event)
                        and _trajectory_agrees(store, previous, event, settings)):
                    target = previous
                    break
        if target is None:
            consolidated.append(event)
        else:
            consolidated[consolidated.index(target)] = _joined(target, event, settings)
            components[target.event_id].extend(components[event.event_id])
            lineage[target.event_id].extend(lineage[event.event_id])
            merged.append({"reason": "synchronous_same_category", "source_event_ids": [target.event_id, event.event_id]})
    consolidated.sort(key=lambda event: (event.start_minute, event.end_minute, event.event_id))
    source_to_final = {
        source: f"v3-e{index:06d}"
        for index, event in enumerate(consolidated, 1)
        for source in lineage[event.event_id]
    }
    for item in merged:
        item["final_event_id"] = source_to_final[item["source_event_ids"][0]]
    return [Event(f"v3-e{index:06d}", event.start_minute, event.end_minute, event.signals)
            for index, event in enumerate(consolidated, 1)], merged


def detect_with_audit(store: FeatureStore, settings: DetectorSettings = DetectorSettings()) -> DetectionResult:
    if settings.service_overlap_policy not in {"city", "related_device"}:
        raise ValueError("unknown service overlap policy")
    if settings.baseline_strategy not in {"global_median", "healthy_tail", "rolling_healthy_tail"}:
        raise ValueError("unknown baseline strategy")
    if settings.cross_city_merge_policy not in {"correlated", "same_city"}:
        raise ValueError("unknown cross-city merge policy")
    count = store.manifest["minute_count"]
    direct_active = np.zeros((count, len(store.nodes)), bool)
    symptom_active: dict[str, np.ndarray] = {}
    target_edges: dict[str, list[int]] = {}
    for index, edge in enumerate(store.edges):
        if edge.get("relation") == "traffic":
            target_edges.setdefault(edge["target"], []).append(index)
            symptom_active.setdefault(edge["target"], np.zeros(count, bool))
    scored: list[tuple[_Rule, np.ndarray, np.ndarray, str]] = []
    for default_rule in _NODE_RULES:
        override = settings.rule_overrides.get(default_rule.feature, {})
        rule = replace(default_rule, **{key: value for key, value in override.items()
                                        if key in {"floor", "absolute"}})
        score_threshold = override.get("score_threshold", settings.score_threshold)
        column = store.feature_index("node", rule.feature)
        if column is None:
            continue
        data = np.asarray(store.node_values[:, :, column], np.float32)
        mask = np.asarray(store.node_mask[:, :, column], bool)
        score, center = _scores(data, mask, rule, settings.baseline_strategy)
        active = _supported(score, score_threshold, rule.immediate)
        score = np.where(active, score, 0)
        direct_active |= active
        scored.append((rule, score, center, "node"))
    for name in store.manifest["features"]["edge"]:
        if not name.startswith("traffic.") or not name.endswith((".error_ratio", ".loss_rate", ".latency_p95_seconds")):
            continue
        column = store.feature_index("edge", name)
        data = np.asarray(store.edge_values[:, :, column], np.float32)
        mask = np.asarray(store.edge_mask[:, :, column], bool)
        absolute = .15 if name.endswith("error_ratio") else .05 if name.endswith("loss_rate") else .15
        rule = _Rule(name, name.split(".")[1], 1, .03 if name.endswith("ratio") else .05, absolute)
        score, center = _scores(data, mask, rule, settings.baseline_strategy)
        if settings.request_aware_service:
            requests_column = store.feature_index("edge", f"traffic.{rule.category}.requests_rate")
            if requests_column is None:
                score = np.zeros_like(score)
            else:
                requests = np.asarray(store.edge_values[:, :, requests_column], np.float32)
                requests_mask = np.asarray(store.edge_mask[:, :, requests_column], bool)
                supported_requests = np.where(requests_mask & np.isfinite(requests) & (requests > 0), requests, 0)
                score *= supported_requests / (supported_requests + 30.0)
        active = _supported(score, settings.score_threshold, False)
        score = np.where(active, score, 0)
        if name.endswith("latency_p95_seconds"):
            # Periodic subsecond latency oscillations are common. A standalone
            # latency event needs a severe value observed by two probes.
            for target, indexes in target_edges.items():
                symptom_active[target] |= ((active[:, indexes] & (data[:, indexes] >= 1.0)).sum(axis=1) >= 2)
        else:
            for target, indexes in target_edges.items():
                symptom_active[target] |= active[:, indexes].any(axis=1)
        scored.append((rule, score, center, "edge"))

    anchors: list[tuple[int, int, str, int | str]] = []
    for entity in range(len(store.nodes)):
        anchors.extend((start, end, "node", entity) for start, end in _groups(direct_active[:, entity], settings))
    for target, active in symptom_active.items():
        uncovered = active.copy()
        city = _target_city(target)
        if city:
            for start, end, kind, entity in anchors:
                if (kind == "node" and store.nodes[int(entity)].startswith(city + "-")
                        and (settings.service_overlap_policy == "city"
                             or "-service-vm-" in store.nodes[int(entity)])):
                    uncovered[start:end] = False
        anchors.extend((start, end, "edge", target) for start, end in _groups(uncovered, settings) if end - start >= 2)
    anchors.sort(key=lambda item: (item[0], item[1], item[2], str(item[3])))
    events: list[Event] = []
    rejected: list[dict] = []
    for start, end, anchor_kind, anchor_entity in anchors:
        signals: list[Signal] = []
        for rule, score, center, kind in scored:
            window = score[start:end]
            if not np.any(window):
                continue
            if kind == "node":
                entities = [int(anchor_entity)] if anchor_kind == "node" else []
            elif anchor_kind == "edge":
                entities = target_edges[str(anchor_entity)]
            else:
                city = store.nodes[int(anchor_entity)].split("-", 1)[0]
                entities = [index for target, indexes in target_edges.items() if _target_city(target) == city for index in indexes]
            for entity in entities:
                if not np.any(window[:, entity]):
                    continue
                offset = int(np.argmax(window[:, entity]))
                minute = start + offset
                value = float((store.node_values if kind == "node" else store.edge_values)[minute, entity, store.feature_index(kind, rule.feature)])
                node = store.nodes[int(entity)] if kind == "node" else store.edges[int(entity)]["target"]
                signals.append(Signal(
                    f"{kind}:{minute}:{int(entity)}:{rule.feature}", minute, node,
                    rule.feature, rule.category, value,
                    float(center[minute, entity] if center.ndim == 2 else center[entity]),
                    float(window[offset, entity]), "direct" if kind == "node" else "symptom",
                ))
        signals.sort(key=lambda item: (-item.score, item.evidence_id))
        if not signals:
            rejected.append({"start_minute": start, "end_minute": end, "reason": "no_valid_signal"})
            continue
        events.append(Event(f"v3-e{len(events) + 1:06d}", start, end, tuple(signals[:settings.max_signals_per_event])))
    if settings.routing_dimension_detection:
        dimension_events = _routing_dimension_events(store, settings)
        for dimension_event in dimension_events:
            if _dominant_direct_category(dimension_event) != "ospf6_interface_flap":
                continue
            node = next(iter(_direct_nodes(dimension_event)))
            events = [event for event in events if not (
                _dominant_direct_category(event) == "ospf6_neighbor_down"
                and _direct_nodes(event) == {node}
                and event.start_minute == dimension_event.start_minute
                and event.end_minute == dimension_event.end_minute
            )]
        events.extend(dimension_events)
    retained: list[Event] = []
    for event in events:
        monitor = next((item for item in event.signals if item.role == "direct" and item.node.endswith("-monitor-vm")), None)
        if monitor is not None:
            city = monitor.node.split("-", 1)[0]
            related = any(
                other is not event
                and other.start_minute <= event.start_minute
                and min(event.end_minute, other.end_minute) - max(event.start_minute, other.start_minute) >= (event.end_minute - event.start_minute) / 2
                and any(
                    signal.role == "direct" and signal.node.startswith(city + "-service-vm-")
                    and signal.category == monitor.category and signal.score > monitor.score
                    for signal in other.signals
                )
                for other in events
            )
            if related:
                rejected.append({"start_minute": event.start_minute, "end_minute": event.end_minute,
                                 "reason": "correlated_weaker_monitor_signal", "node": monitor.node})
                continue
        retained.append(Event(f"v3-e{len(retained) + 1:06d}", event.start_minute, event.end_minute, event.signals))
    final, merged = _consolidate(store, retained, settings)
    if settings.weak_cpu_max_minutes > 0 and settings.weak_cpu_max_score > 0:
        screened: list[Event] = []
        for event in final:
            direct = [signal for signal in event.signals if signal.role == "direct"]
            weak_cpu_only = (
                event.end_minute - event.start_minute <= settings.weak_cpu_max_minutes
                and bool(direct)
                and {signal.category for signal in direct} == {"cpu_pressure"}
                and len({signal.node for signal in direct}) == 1
                and not any(signal.role == "symptom" for signal in event.signals)
                and max(signal.score for signal in direct) < settings.weak_cpu_max_score
            )
            if weak_cpu_only:
                rejected.append({"event_id": event.event_id, "start_minute": event.start_minute,
                                 "end_minute": event.end_minute, "reason": "short_weak_single_cpu_signal"})
            else:
                screened.append(event)
        final = screened
    return DetectionResult(tuple(final), tuple(rejected), tuple(merged))


def detect(store: FeatureStore, settings: DetectorSettings = DetectorSettings()) -> list[Event]:
    return list(detect_with_audit(store, settings).events)

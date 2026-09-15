"""Split time-overlapping but causally disconnected city incidents."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from ..preprocessing.observations import AnomalyEvidence, DetectedEvent
from .robust_detector import (
    _energies,
    _qualified_trigger_evidence,
    confidence_from_peak_energy,
)


def _city(node_id: str | None, cities: tuple[str, ...]) -> str | None:
    if node_id is None:
        return None
    for city in cities:
        if node_id.startswith(city + "-"):
            return city
    return None


def split_concurrent_events(
    events: Iterable[DetectedEvent],
    cities: Iterable[str],
    *,
    config: dict[str, Any] | None = None,
) -> list[DetectedEvent]:
    """Split independent city components while preserving relational bridges."""
    city_values = tuple(cities)
    detector_config = config or {}
    open_threshold = float(detector_config.get("open_threshold", 7.0))
    result: list[DetectedEvent] = []
    for original in events:
        present = {
            city
            for point in original.evidence
            if (city := _city(point.node_id, city_values)) is not None
        }
        if len(present) <= 1:
            result.append(original)
            continue

        parent = {city: city for city in present}

        def find(city: str) -> str:
            while parent[city] != city:
                parent[city] = parent[parent[city]]
                city = parent[city]
            return city

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        for point in original.evidence:
            source_city = _city(point.node_id, city_values)
            if source_city not in present or point.event_role != "trigger":
                continue
            for related in point.related_node_ids:
                related_city = _city(related, city_values)
                if related_city in present:
                    union(source_city, related_city)

        grouped: dict[str, list[AnomalyEvidence]] = defaultdict(list)
        unassigned: list[AnomalyEvidence] = []
        for point in original.evidence:
            point_city = _city(point.node_id, city_values)
            if point_city in present:
                grouped[find(point_city)].append(point)
            else:
                unassigned.append(point)

        if len(grouped) <= 1:
            result.append(original)
            continue
        # Global/unknown evidence is copied to each component as context; it
        # cannot by itself join otherwise unrelated city incidents.
        for points in grouped.values():
            points.extend(unassigned)
            points.sort(key=lambda point: (point.timestamp, -point.score, point.metric))
            raw_triggers = tuple(
                point for point in points if point.event_role == "trigger"
            )
            if not raw_triggers:
                continue
            triggers = (
                _qualified_trigger_evidence(raw_triggers, detector_config)
                if config is not None
                else raw_triggers
            )
            if not triggers:
                continue
            trigger_minutes = sorted(
                {
                    point.timestamp.replace(second=0, microsecond=0)
                    for point in triggers
                }
            )
            trigger_energy, _ = _energies(
                triggers, trigger_minutes, detector_config
            )
            peak_time = max(
                trigger_minutes,
                key=lambda minute: (
                    trigger_energy[minute],
                    -minute.timestamp(),
                ),
            )
            peak_energy = trigger_energy[peak_time]
            if peak_energy < open_threshold:
                continue
            start = original.start
            end = original.end
            result.append(
                DetectedEvent(
                    start=start,
                    end=end,
                    peak_time=min(max(peak_time, start), end),
                    confidence=confidence_from_peak_energy(
                        peak_energy, open_threshold
                    ),
                    evidence=tuple(points),
                    source_counts=dict(sorted(Counter(point.source for point in points).items())),
                )
            )
    return sorted(result, key=lambda event: (event.start, event.end, event.peak_time))

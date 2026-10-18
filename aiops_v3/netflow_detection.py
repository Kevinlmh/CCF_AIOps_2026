"""Detect service-wide and path-specific response loss from NetFlow statistics.

The detector only consumes the compact ``minute x service x observer`` rows
produced by :mod:`aiops_v3.netflow_stats`.  It never reads raw CSVs and never
guesses a fault when a minute has no service observation at all.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import statistics

_SERVICES = ("dns", "web", "auth")

SERVICE_NODE = {
    "dns": "beida-service-vm-1",
    "web": "shanghai-service-vm-1",
    "auth": "wuhan-service-vm-1",
}

# Rule category prior for a healthy-vs-failing service endpoint.  The value is
# only a prior: the role layer may override it with cited mechanism evidence.
SERVICE_CATEGORY = {
    "dns": ("service", "dns_down"),
    "web": ("service", "web_5xx"),
    "auth": ("service", "auth_timeout"),
}

PATH_CATEGORY = ("link", "loss")


@dataclass(frozen=True)
class NetflowSettings:
    min_ratio: float = 0.5
    min_requests: float = 100.0
    min_minutes: int = 1
    max_event_minutes: int = 30
    min_observer_requests: float = 20.0
    min_subset_minutes: int = 3
    min_volume_minutes: int = 3
    volume_drop_ratio: float = 0.2
    volume_recovery_ratio: float = 0.5
    require_observed_minutes: bool = True


@dataclass(frozen=True)
class NetflowEvent:
    start_minute: int
    end_minute: int  # exclusive
    service: str
    service_node: str
    scope: str  # "all" | "subset"
    observers: tuple[dict, ...]
    baseline_ratio: float
    window_ratio: float
    affected_ratio: float
    affected_cities: tuple[str, ...]
    kind: str = "response_loss"  # "response_loss" | "volume_loss"

    @property
    def duration(self) -> int:
        return self.end_minute - self.start_minute


def _group_rows(rows):
    """Return per-minute service totals and per-observer cells."""
    totals: dict[str, dict[int, list[float]]] = {service: {} for service in _SERVICES}
    cells: dict[tuple[str, int, str], list[float]] = {}
    for row in rows:
        service = row.get("service")
        if service not in totals:
            continue
        minute = int(row["minute"])
        req = float(row.get("req_pkts") or 0.0)
        resp = float(row.get("resp_pkts") or 0.0)
        total = totals[service].setdefault(minute, [0.0, 0.0])
        total[0] += req
        total[1] += resp
        cell = cells.setdefault((service, minute, str(row["observer_city"])), [0.0, 0.0])
        cell[0] += req
        cell[1] += resp
    return totals, cells


def _runs(minutes, gap: int = 1):
    ordered = sorted(minutes)
    if not ordered:
        return []
    spans: list[tuple[int, int]] = []
    begin = previous = ordered[0]
    for minute in ordered[1:]:
        if minute - previous > gap + 1:
            spans.append((begin, previous + 1))
            begin = minute
        previous = minute
    spans.append((begin, previous + 1))
    return spans


def _split(start: int, end: int, maximum: int):
    return [(part, min(end, part + maximum)) for part in range(start, end, maximum)]


def _window_observers(service, start, end, cells, settings):
    rows = []
    for (cell_service, minute, city), (req, resp) in cells.items():
        if cell_service != service or not start <= minute < end:
            continue
        rows.append((city, req, resp))
    grouped: dict[str, list[float]] = {}
    for city, req, resp in rows:
        grouped.setdefault(city, [0.0, 0.0])
        grouped[city][0] += req
        grouped[city][1] += resp
    observers = []
    for city in sorted(grouped):
        req, resp = grouped[city]
        ratio = resp / req if req > 0 else 0.0
        observers.append({
            "kind": "netflow",
            "source_city": city,
            "target": SERVICE_NODE[service],
            "req": round(req, 3),
            "resp": round(resp, 3),
            "ratio": round(ratio, 6),
            "stance": "support" if (req >= settings.min_observer_requests and ratio < settings.min_ratio) else "counter",
        })
    return tuple(observers)


def detect(rows, *, settings: NetflowSettings = NetflowSettings(),
           minute_count: int | None = None) -> tuple[NetflowEvent, ...]:
    """Return response-loss events.  Deterministic and order independent."""
    totals, cells = _group_rows(list(rows))
    events: list[NetflowEvent] = []
    for service in _SERVICES:
        by_minute = totals[service]
        observed = {minute for minute in by_minute if by_minute[minute][0] + by_minute[minute][1] > 0}
        if not observed:
            continue
        eligible = [minute for minute in observed if by_minute[minute][0] >= settings.min_requests]
        if not eligible:
            continue
        ratios = [by_minute[minute][1] / by_minute[minute][0] for minute in eligible]
        baseline_ratio = statistics.median(ratios)
        median_requests = statistics.median([by_minute[minute][0] for minute in eligible])
        low = [
            minute for minute in observed
            if by_minute[minute][0] >= max(settings.min_requests, 0.2 * median_requests)
            and by_minute[minute][1] / by_minute[minute][0] < settings.min_ratio
        ]
        observer_low: dict[str, set[int]] = {}
        for (cell_service, minute, city), (req, resp) in cells.items():
            if cell_service != service or minute not in observed:
                continue
            if req >= settings.min_observer_requests and resp / req < settings.min_ratio:
                observer_low.setdefault(city, set()).add(minute)
        candidates = set(low)
        for minutes in observer_low.values():
            candidates |= minutes
        for start, end in _runs(candidates):
            for part_start, part_end in _split(start, end, settings.max_event_minutes):
                if part_end - part_start < settings.min_minutes:
                    continue
                if settings.require_observed_minutes and any(
                        minute not in observed for minute in range(part_start, part_end)):
                    continue
                if minute_count is not None and not (0 <= part_start < part_end <= minute_count):
                    continue
                window_req = sum(by_minute[minute][0] for minute in range(part_start, part_end))
                window_resp = sum(by_minute[minute][1] for minute in range(part_start, part_end))
                if window_req <= 0:
                    continue
                window_ratio = window_resp / window_req
                observers = _window_observers(service, part_start, part_end, cells, settings)
                affected = tuple(
                    city for city in sorted(observer_low)
                    if len(observer_low[city] & set(range(part_start, part_end))) >= settings.min_minutes
                    and any(row["source_city"] == city and row["req"] >= settings.min_observer_requests
                            for row in observers)
                )
                if len(affected) >= 2:
                    scope = "all"
                elif len(affected) == 1:
                    scope = "subset"
                    if part_end - part_start < settings.min_subset_minutes:
                        continue
                else:
                    continue
                affected_ratio = min(
                    (row["resp"] / row["req"] for row in observers
                     if row["source_city"] in affected and row["req"] > 0),
                    default=window_ratio,
                )
                events.append(NetflowEvent(
                    start_minute=part_start,
                    end_minute=part_end,
                    service=service,
                    service_node=SERVICE_NODE[service],
                    scope=scope,
                    kind="response_loss",
                    observers=observers,
                    baseline_ratio=round(baseline_ratio, 6),
                    window_ratio=round(window_ratio, 6),
                    affected_ratio=round(affected_ratio, 6),
                    affected_cities=affected,
                ))
    # City reachability pass: two or more services lose most of the request
    # volume for the SAME observer city at the same time.  A single service's
    # volume fluctuates naturally, but losing several services together points
    # at a path/link/firewall fault in that city.  Service-wide traffic stops
    # are excluded because they also hit the global volume.
    if settings.min_volume_minutes > 0:
        city_service_minutes: dict[str, dict[str, dict[int, float]]] = {}
        baselines: dict[tuple[str, str], float] = {}
        service_global: dict[str, dict[int, float]] = {}
        for service in _SERVICES:
            by_minute = totals[service]
            observed = {minute for minute in by_minute
                        if by_minute[minute][0] + by_minute[minute][1] > 0}
            if not observed:
                continue
            for minute in observed:
                service_global.setdefault(service, {})[minute] = by_minute[minute][0]
            for (cell_service, minute, city), (req, _resp) in cells.items():
                if cell_service != service:
                    continue
                city_service_minutes.setdefault(city, {}).setdefault(service, {})[minute] = req
            for city in city_service_minutes:
                minutes = city_service_minutes[city].get(service)
                if not minutes:
                    continue
                active = [minute for minute in minutes if minutes[minute] >= settings.min_observer_requests]
                if len(active) >= 10:
                    baselines[(city, service)] = statistics.median([minutes[minute] for minute in active])
        service_median = {service: statistics.median(list(values.values()))
                          for service, values in service_global.items() if values}
        for city in sorted(city_service_minutes):
            services = [service for service in _SERVICES if (city, service) in baselines]
            if len(services) < 2:
                continue
            global_minutes = sorted(set().union(
                *(set(service_global.get(service, {})) for service in services)))
            if not global_minutes:
                continue
            low_by_service: dict[str, set[int]] = {}
            for service in services:
                minutes = city_service_minutes[city][service]
                baseline = baselines[(city, service)]
                low_by_service[service] = {
                    minute for minute in global_minutes
                    if minutes.get(minute, 0.0) < settings.volume_drop_ratio * baseline
                }
            low_minutes = [
                minute for minute in global_minutes
                if sum(1 for service in services if minute in low_by_service[service]) >= 2
            ]
            for start, end in _runs(low_minutes):
                for part_start, part_end in _split(start, end, settings.max_event_minutes):
                    if part_end - part_start < settings.min_volume_minutes:
                        continue
                    window = range(part_start, part_end)
                    normal_services = 0
                    for service in services:
                        global_values = [service_global[service].get(minute, 0.0) for minute in window]
                        if not global_values:
                            continue
                        if statistics.median(global_values) >= 0.5 * service_median.get(service, 0.0):
                            normal_services += 1
                    if normal_services < 2:
                        continue
                    recovered = False
                    for service in services:
                        minutes = city_service_minutes[city][service]
                        baseline = baselines[(city, service)]
                        before = any(minutes.get(minute, 0.0) >= settings.volume_recovery_ratio * baseline
                                     for minute in range(max(0, part_start - 10), part_start))
                        after = any(minutes.get(minute, 0.0) >= settings.volume_recovery_ratio * baseline
                                    for minute in range(part_end, min(max(global_minutes) + 1, part_end + 10)))
                        if before and after:
                            recovered = True
                            break
                    if not recovered:
                        continue
                    low_services = [service for service in services
                                    if sum(1 for minute in window if minute in low_by_service[service])
                                    >= max(2, settings.min_volume_minutes)]
                    if len(low_services) < 2:
                        continue
                    ratios = [city_service_minutes[city][service].get(minute, 0.0)
                              / baselines[(city, service)]
                              for service in low_services for minute in window]
                    affected_ratio = min(ratios) if ratios else 0.0
                    events.append(NetflowEvent(
                        start_minute=part_start, end_minute=part_end,
                        service="path", service_node=f"{city}-br-1",
                        scope="subset", observers=_window_observers(
                            low_services[0], part_start, part_end, cells, settings),
                        baseline_ratio=1.0, window_ratio=affected_ratio,
                        affected_ratio=affected_ratio, affected_cities=(city,),
                        kind="volume_loss",
                    ))
    events.sort(key=lambda item: (item.start_minute, item.end_minute, item.service))
    return tuple(events)


def signal_rows(event: NetflowEvent) -> tuple[dict, ...]:
    """Convert an event into Signal-compatible dicts for the detection layer.

    ``all`` scope: one direct signal on the target service node.
    ``subset`` scope: a symptom signal on the target plus context signals on the
    affected cities' border routers, so path devices can enter the candidate set.
    """
    signals: list[dict] = []
    if event.kind == "volume_loss":
        city = event.affected_cities[0] if event.affected_cities else event.service_node.split("-", 1)[0]
        prefix = f"netflow:{event.start_minute}:{city}:path"
        return (
            {"evidence_id": f"{prefix}:direct", "minute": event.start_minute,
             "node": f"{city}-br-1", "feature": "netflow.path.request_volume_ratio",
             "category": "path_unreachable", "value": event.affected_ratio,
             "reference": 1.0, "score": 5.0, "role": "direct"},
            {"evidence_id": f"{prefix}:br2", "minute": event.start_minute,
             "node": f"{city}-br-2", "feature": "netflow.path.request_volume_ratio",
             "category": "path_unreachable", "value": event.affected_ratio,
             "reference": 1.0, "score": 4.0, "role": "context"},
            {"evidence_id": f"{prefix}:cr1", "minute": event.start_minute,
             "node": f"{city}-cr-1", "feature": "netflow.path.request_volume_ratio",
             "category": "path_unreachable", "value": event.affected_ratio,
             "reference": 1.0, "score": 4.0, "role": "context"},
            {"evidence_id": f"{prefix}:fw", "minute": event.start_minute,
             "node": f"{city}-fw", "feature": "netflow.path.request_volume_ratio",
             "category": "path_unreachable", "value": event.affected_ratio,
             "reference": 1.0, "score": 4.0, "role": "context"},
        )
    if event.scope == "all":
        signals.append({
            "evidence_id": f"netflow:{event.start_minute}:{event.service}:{event.service_node}",
            "minute": event.start_minute,
            "node": event.service_node,
            "feature": f"netflow.{event.service}.response_ratio",
            "category": "service_down",
            "value": event.affected_ratio,
            "reference": event.baseline_ratio,
            "score": 4.0 + 6.0 * max(0.0, 1.0 - event.affected_ratio),
            "role": "direct",
        })
        return tuple(signals)
    signals.append({
        "evidence_id": f"netflow:{event.start_minute}:{event.service}:{event.service_node}",
        "minute": event.start_minute,
        "node": event.service_node,
        "feature": "netflow.path.response_ratio",
        "category": "path_unreachable",
        "value": event.affected_ratio,
        "reference": event.baseline_ratio,
        "score": 4.0 + 6.0 * max(0.0, 1.0 - event.affected_ratio),
        "role": "symptom",
    })
    for city in event.affected_cities:
        signals.append({
            "evidence_id": f"netflow:{event.start_minute}:{city}:{event.service}",
            "minute": event.start_minute,
            "node": f"{city}-br-1",
            "feature": "netflow.path.response_ratio",
            "category": "path_unreachable",
            "value": event.affected_ratio,
            "reference": event.baseline_ratio,
            "score": 4.0 + 4.0 * max(0.0, 1.0 - event.affected_ratio),
            "role": "context",
        })
    return tuple(signals)


def with_settings(event: NetflowEvent, **changes) -> NetflowEvent:
    return replace(event, **changes)

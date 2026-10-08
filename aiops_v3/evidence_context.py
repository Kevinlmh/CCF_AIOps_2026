"""Compact observed device timelines; absence of data remains explicit."""

from __future__ import annotations

import hashlib
import json

import numpy as np

from .detection import _NODE_RULES


def _metric(feature, times, values, event, count, dimensions=None):
    times, values = np.asarray(times), np.asarray(values)
    finite = np.isfinite(values)
    times, values = times[finite], values[finite]
    row = {"feature": feature, "dimensions": dimensions or {}}
    windows = {"before": (max(0, event.start_minute - 10), event.start_minute),
               "during": (event.start_minute, event.end_minute),
               "after": (event.end_minute, min(count, event.end_minute + 5))}
    for name, (start, end) in windows.items():
        selected = values[(times >= start) & (times < end)]
        row[name] = {"observed": len(selected), "expected": end - start,
                     "median": float(np.median(selected)) if len(selected) else None,
                     "min": float(np.min(selected)) if len(selected) else None,
                     "max": float(np.max(selected)) if len(selected) else None}
    row.update(supported_category=None, onset_minute=None, recovery_minute=None)
    before = values[(times >= windows["before"][0]) & (times < event.start_minute)]
    rule = next((r for r in _NODE_RULES if r.feature == feature), None)
    if rule is None or len(before) < 3:
        return row
    reference = float(np.median(before))
    scale = max(rule.floor, 1.4826 * float(np.median(np.abs(before - reference))))
    abnormal = ((values - reference) * rule.direction >= 4 * scale)
    abnormal &= values >= rule.absolute if rule.direction > 0 else values <= rule.absolute
    if not rule.immediate:
        adjacent = (np.diff(times) == 1) & abnormal[:-1] & abnormal[1:]
        abnormal &= np.r_[False, adjacent] | np.r_[adjacent, False]
    active = times[abnormal & (times >= event.start_minute) & (times < event.end_minute)]
    if len(active):
        row["supported_category"] = rule.category
        row["onset_minute"] = int(active[0])
        # Follow the observed tail until recovery. A missing minute breaks the claim.
        last = int(active[-1])
        for minute in range(last + 1, windows["after"][1]):
            observed = np.flatnonzero(times == minute)
            if not len(observed):
                break
            if not abnormal[observed[0]]:
                row["recovery_minute"] = minute
                break
    return row


def diagnostic_context(store, event, candidates):
    count = store.manifest["minute_count"]
    start, end = max(0, event.start_minute - 10), min(count, event.end_minute + 5)
    nodes = {c.node for c in candidates}
    index = {node: i for i, node in enumerate(getattr(store, "nodes", []))}
    event_features = {s.feature for s in event.signals}
    companion = ["node.cpu_usage", "node.process_count", "node.memory_available_ratio",
                 "node.disk_io_util", "node.filesystem_used_ratio", "scrape.scrape_up",
                 "routing.bgp_peer_up", "routing.ospf6_neighbor_state_code"]
    cards = []
    for node in sorted(nodes):
        metrics = []
        if node in index and hasattr(store, "node_mask"):
            names = store.manifest["features"]["node"]
            selected = sorted(names, key=lambda f: (f not in event_features,
                              companion.index(f) if f in companion else len(companion), f))
            selected = [f for f in selected if f in event_features or f.startswith(
                ("node.", "routing.", "scrape."))][:8]
            for feature in selected:
                column, entity = names.index(feature), index[node]
                valid = store.node_mask[start:end, entity, column]
                if not np.any(valid):
                    continue
                row = _metric(feature, np.arange(start, end)[valid],
                              store.node_values[start:end, entity, column][valid], event, count)
                row["evidence_id"] = f"context:node:{node}:{feature}"
                metrics.append(row)
        if hasattr(store, "iter_dimension_series"):
            dimension_rows = []
            for series in store.iter_dimension_series(node_id=node):
                if series.node_id != node or not (series.metric in event_features or series.metric.startswith(
                        ("routing.", "interface."))):
                    continue
                valid = (series.time_indices >= start) & (series.time_indices < end)
                if not np.any(valid):
                    continue
                dimensions = dict(series.dimensions)
                row = _metric(series.metric, series.time_indices[valid], series.values[valid],
                              event, count, dimensions)
                identity = json.dumps([node, series.metric, dimensions], sort_keys=True)
                row["evidence_id"] = "context:dimension:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
                dimension_rows.append(row)
            dimension_rows.sort(key=lambda r: (r["supported_category"] is None, r["evidence_id"]))
            metrics.extend(dimension_rows[:6])
        cards.append({"node": node, "role": "observer" if node.endswith("traffic-vm") else "device",
                      "metrics": metrics})
    text = []
    if hasattr(store, "iter_text_events"):
        seen = set()
        for item in store.iter_text_events(start=store.time_at(start), end=store.time_at(end), node_ids=nodes):
            if item["evidence_id"] in seen:
                continue
            seen.add(item["evidence_id"])
            text.append({**item, "message": str(item.get("message", ""))[:400]})
            if len(text) >= 20:
                break
    sources = tuple(sorted(k for k, v in store.manifest.get("source_counts", {}).items() if v > 0))
    return tuple(cards), tuple(text), sources

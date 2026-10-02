"""Inspect per-peer and per-interface route observations before promoting rules."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .store import open_store


ROUTING_METRICS = (
    "routing.bgp_peer_prefix_received",
    "routing.bgp_peer_up",
    "routing.bgp_peer_uptime_seconds",
    "routing.ipv6_route_count",
    "routing.ipv6_route_change_total",
    "routing.ipv6_route_exists",
    "routing.ospf6_interface_enabled",
    "routing.ospf6_interface_cost",
    "routing.ospf6_neighbor_state_code",
)


def audit_routing_dimensions(store) -> dict:
    metrics = {name: {"series": 0, "varying_series": 0, "examples": []}
               for name in ROUTING_METRICS}
    for series in store.iter_dimension_series(source="routing"):
        entry = metrics.get(series.metric)
        if entry is None:
            continue
        entry["series"] += 1
        if len(series.values) < 2 or not np.any(np.diff(series.values)):
            continue
        entry["varying_series"] += 1
        if len(entry["examples"]) < 30:
            first = int(np.flatnonzero(np.diff(series.values))[0])
            entry["examples"].append({
                "node": series.node_id,
                "dimensions": dict(series.dimensions),
                "first_change_minute": int(series.time_indices[first + 1]),
                "minimum": float(np.min(series.values)),
                "maximum": float(np.max(series.values)),
            })
    return {
        "metrics": metrics,
        "classification_limits": {
            "bgp_route_filter": "requires stable peer and corroborating route-count loss",
            "ospf6_interface_flap": "requires interface-state transition with neighbor or cost evidence",
            "route_loop": "no verified topology or path-loop observation in the current store",
            "long_path_interruption": "no per-path latency or trace observation in the second batch",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit routing dimension series")
    parser.add_argument("--input-store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_routing_dimensions(open_store(args.input_store))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({name: item["varying_series"] for name, item in report["metrics"].items()}))


if __name__ == "__main__":
    main()

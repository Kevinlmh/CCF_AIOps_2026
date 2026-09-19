"""Safely replay an older evidence checkpoint with the current event segmenter.

This is an audit tool, not a production-cache bypass: it writes a new event
checkpoint with provenance metadata and never changes the source evidence file.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiops_challenge_2026.config import load_public_config
from baseline.bian.anomaly_detector.robust_detector import (
    retain_evidence,
    segment_evidence_by_city,
)
from baseline.bian.checkpoint import load_evidence_checkpoint, save_event_checkpoint
from baseline.bian.preprocessing.metric_semantics import MetricSemantics


def resegment(
    evidence_path: Path,
    output_path: Path,
    config_path: Path,
) -> dict[str, Any]:
    model = json.loads(config_path.read_text(encoding="utf-8"))
    detector = model["detector"]
    points, bounds, coverage, metadata = load_evidence_checkpoint(evidence_path)
    semantics = MetricSemantics.from_json(
        ROOT / "baseline/bian/config/metric_semantics.json"
    )
    remapped = tuple(
        replace(point, event_role="support")
        if point.event_role == "trigger"
        and semantics.rule_for(point.metric).event_role == "support"
        else point
        for point in points
    )
    retained = tuple(point for point in remapped if retain_evidence(point, detector))
    cities = tuple(load_public_config("network_elements")["cities"])
    events, diagnostics = segment_evidence_by_city(
        retained,
        cities=cities,
        observation_start=bounds[0],
        observation_end=bounds[1],
        config=detector,
        source_coverage=coverage,
    )
    save_event_checkpoint(
        events,
        output_path,
        metadata={
            "model_version": model.get("version", "unknown"),
            "audit_resegmentation": True,
            "source_evidence": str(evidence_path.resolve()),
            "source_model_version": metadata.get("model_version"),
        },
    )
    event_cities = Counter(
        next(
            (
                city
                for city in cities
                if any(
                    point.node_id == city
                    or (point.node_id or "").startswith(city + "-")
                    for point in event.evidence
                )
            ),
            "unknown",
        )
        for event in events
    )
    driver_metrics: Counter[str] = Counter()
    duration_buckets: Counter[str] = Counter()
    city_span: Counter[int] = Counter()
    support_only_events = 0
    for event in events:
        triggers = [point for point in event.evidence if point.event_role == "trigger"]
        if not triggers:
            support_only_events += 1
        else:
            driver_metrics[max(triggers, key=lambda point: point.score).metric] += 1
        duration = (event.end - event.start).total_seconds() / 60.0
        duration_buckets[
            "le_2" if duration <= 2 else "3_to_4" if duration < 5 else "ge_5"
        ] += 1
        represented = {
            city
            for point in event.evidence
            for city in cities
            if (point.node_id or "").startswith(city + "-")
        }
        city_span[len(represented)] += 1
    return {
        "model_version": model.get("version", "unknown"),
        "source_model_version": metadata.get("model_version"),
        "evidence_before": len(points),
        "evidence_after_zero_suppression": len(retained),
        "events": len(events),
        "events_by_city": dict(sorted(event_cities.items())),
        "driver_metrics": dict(driver_metrics.most_common()),
        "duration_buckets": dict(sorted(duration_buckets.items())),
        "event_city_span": {str(key): value for key, value in sorted(city_span.items())},
        "support_only_events": support_only_events,
        "nonzero_trigger_minutes": sum(
            value > 0 for value in diagnostics.trigger_minute_energy.values()
        ),
        "total_minutes": len(diagnostics.trigger_minute_energy),
        "output": str(output_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "baseline/bian/config/model_v1.json",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = resegment(args.evidence, args.output, args.config)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

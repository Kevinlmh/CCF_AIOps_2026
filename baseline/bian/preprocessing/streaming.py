"""End-to-end bounded-memory ingestion and anomaly detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..anomaly_detector.robust_detector import (
    DetectionDiagnostics,
    _text_evidence,
    segment_evidence,
)
from ..anomaly_detector.streaming_detector import OnlineRobustDetector
from .metric_semantics import CounterTransformer, MetricSemantics
from .multisource import (
    SOURCE_ORDER,
    ObservationBundle,
    iter_file_observations,
    iter_source_files,
    validate_source_inventory,
)
from .observations import NumericObservation, ParseStats, TextEvent, city_from_path


HERE = Path(__file__).resolve().parent
DEFAULT_SEMANTICS = HERE.parent / "config" / "metric_semantics.json"


@dataclass(frozen=True, slots=True)
class StreamingDetectionResult:
    events: tuple
    diagnostics: DetectionDiagnostics
    bundle: ObservationBundle
    observation_count: int
    series_state_count: int
    dropped_evidence_count: int
    dropped_evidence_by_role: dict[str, int]
    evidence: tuple


def _deduplicate_text_events(events: Iterable[TextEvent]) -> tuple[TextEvent, ...]:
    unique: dict[tuple[object, ...], TextEvent] = {}
    for event in events:
        minute = event.timestamp.replace(second=0, microsecond=0)
        key = (
            minute,
            event.source,
            event.node_id,
            event.program,
            event.event_family,
            event.severity,
            event.message,
        )
        unique.setdefault(key, event)
    return tuple(sorted(unique.values(), key=lambda item: (item.timestamp, item.node_id or "", item.event_family)))


def detect_events_streaming(
    root: Path,
    aliases: dict[str, str],
    valid_roles: Iterable[str],
    detector_config: dict[str, Any],
    *,
    semantics_path: Path = DEFAULT_SEMANTICS,
    scratch_dir: Path | None = None,
    strict_cities: Iterable[str] | None = None,
    strict_sources: Iterable[str] = SOURCE_ORDER,
    segment_cities: Iterable[str] | None = None,
) -> StreamingDetectionResult:
    """Scan all source rows, retaining rolling state and abnormal evidence only."""
    if strict_cities is not None:
        validate_source_inventory(
            root,
            aliases,
            expected_cities=tuple(strict_cities),
            expected_sources=tuple(strict_sources),
        )

    roles = tuple(valid_roles)
    stats = ParseStats()
    discovered: set[str] = set()
    detector = OnlineRobustDetector(detector_config)
    transformer = CounterTransformer(MetricSemantics.from_json(semantics_path))
    text_events: list[TextEvent] = []
    observation_start: datetime | None = None
    observation_end: datetime | None = None

    for source, path in iter_source_files(root):
        discovered.add(source)
        stats.record_file(source)
        city = city_from_path(path, aliases)
        for item in iter_file_observations(
            path,
            source,
            city,
            roles,
            stats,
            scratch_dir=scratch_dir,
            detector_config=detector_config,
        ):
            if observation_start is None or item.timestamp < observation_start:
                observation_start = item.timestamp
            if observation_end is None or item.timestamp > observation_end:
                observation_end = item.timestamp
            if isinstance(item, TextEvent):
                text_events.append(item)
                continue
            assert isinstance(item, NumericObservation)
            for transformed in transformer.transform(item):
                detector.add(transformed)

    coverage = {
        source: stats.rows_by_source.get(source, 0)
        for source in SOURCE_ORDER
        if source in discovered
    }
    texts = _deduplicate_text_events(text_events)
    bundle = ObservationBundle((), texts, stats, coverage)
    evidence = list(detector.finalize())
    evidence.extend(_text_evidence(bundle, detector_config))

    if observation_start is None or observation_end is None:
        diagnostics = DetectionDiagnostics(
            minute_energy={},
            trigger_minute_energy={},
            source_energy={},
            evidence_count=0,
            source_coverage=coverage,
            observation_start=None,
            observation_end=None,
            open_threshold=float(detector_config.get("open_threshold", 7.0)),
            keep_threshold=float(detector_config.get("keep_threshold", 3.0)),
        )
        events = []
    else:
        if segment_cities is None:
            events, diagnostics = segment_evidence(
                evidence,
                observation_start=observation_start,
                observation_end=observation_end,
                config=detector_config,
                source_coverage=coverage,
            )
        else:
            from ..anomaly_detector.robust_detector import segment_evidence_by_city

            events, diagnostics = segment_evidence_by_city(
                evidence,
                cities=segment_cities,
                observation_start=observation_start,
                observation_end=observation_end,
                config=detector_config,
                source_coverage=coverage,
            )
    return StreamingDetectionResult(
        events=tuple(events),
        diagnostics=diagnostics,
        bundle=bundle,
        observation_count=detector.observation_count,
        series_state_count=detector.series_state_count,
        dropped_evidence_count=detector.dropped_evidence_count,
        dropped_evidence_by_role=detector.dropped_evidence_by_role,
        evidence=tuple(evidence),
    )

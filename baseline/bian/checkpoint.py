"""Portable event checkpoint between CPU preprocessing and LLM inference."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from .preprocessing.observations import AnomalyEvidence, DetectedEvent, parse_time


FORMAT_VERSION = 1
EVIDENCE_FORMAT_VERSION = 1


def _time(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _serialize_evidence(item: AnomalyEvidence) -> dict[str, Any]:
    return {
        "timestamp": _time(item.timestamp),
        "source": item.source,
        "node_id": item.node_id,
        "related_node_ids": list(item.related_node_ids),
        "metric": item.metric,
        "value": item.value,
        "baseline": item.baseline,
        "score": item.score,
        "direction": item.direction,
        "dimensions": [list(pair) for pair in item.dimensions],
        "summary": item.summary,
        "event_role": item.event_role,
        "semantic_score": item.semantic_score,
    }


def _deserialize_evidence(item: dict[str, Any]) -> AnomalyEvidence:
    return AnomalyEvidence(
        timestamp=_required_time(item.get("timestamp"), "evidence.timestamp"),
        source=str(item.get("source", "")),
        node_id=item.get("node_id"),
        related_node_ids=tuple(item.get("related_node_ids", [])),
        metric=str(item.get("metric", "")),
        value=float(item.get("value")),
        baseline=float(item.get("baseline")),
        score=float(item.get("score")),
        direction=item.get("direction"),
        dimensions=tuple(tuple(pair) for pair in item.get("dimensions", [])),
        summary=item.get("summary"),
        event_role=str(item.get("event_role", "trigger")),
        semantic_score=float(item.get("semantic_score", 0.0)),
    )


def _write_atomic_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def save_event_checkpoint(
    events: Iterable[DetectedEvent],
    path: Path,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    payload = {
        "format_version": FORMAT_VERSION,
        "metadata": dict(metadata or {}),
        "events": [
            {
                "start": _time(event.start),
                "end": _time(event.end),
                "peak_time": _time(event.peak_time),
                "confidence": event.confidence,
                "source_counts": event.source_counts,
                "evidence": [_serialize_evidence(item) for item in event.evidence],
            }
            for event in events
        ],
    }
    _write_atomic_json(payload, path)


def _required_time(value: Any, field: str):
    parsed = parse_time(value if isinstance(value, str) else None)
    if parsed is None:
        raise ValueError(f"invalid checkpoint timestamp: {field}")
    return parsed


def load_event_checkpoint(path: Path) -> tuple[list[DetectedEvent], dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read event checkpoint: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported event checkpoint version")
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("checkpoint events must be a list")
    events: list[DetectedEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict) or not isinstance(raw.get("evidence"), list):
            raise ValueError("invalid checkpoint event")
        evidence = tuple(_deserialize_evidence(item) for item in raw["evidence"])
        events.append(
            DetectedEvent(
                start=_required_time(raw.get("start"), "event.start"),
                end=_required_time(raw.get("end"), "event.end"),
                peak_time=_required_time(raw.get("peak_time"), "event.peak_time"),
                confidence=float(raw.get("confidence")),
                evidence=evidence,
                source_counts={
                    str(key): int(value)
                    for key, value in dict(raw.get("source_counts", {})).items()
                },
            )
        )
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("checkpoint metadata must be an object")
    return events, metadata


def save_evidence_checkpoint(
    evidence: Iterable[AnomalyEvidence],
    path: Path,
    *,
    observation_start,
    observation_end,
    source_coverage: dict[str, int],
    metadata: dict[str, Any] | None = None,
) -> None:
    if observation_end < observation_start:
        raise ValueError("evidence checkpoint observation bounds are reversed")
    payload = {
        "format": "bian-evidence",
        "format_version": EVIDENCE_FORMAT_VERSION,
        "metadata": dict(metadata or {}),
        "observation_start": _time(observation_start),
        "observation_end": _time(observation_end),
        "source_coverage": {
            str(source): int(count) for source, count in source_coverage.items()
        },
        "evidence": [_serialize_evidence(item) for item in evidence],
    }
    _write_atomic_json(payload, path)


def load_evidence_checkpoint(
    path: Path,
) -> tuple[
    tuple[AnomalyEvidence, ...],
    tuple[Any, Any],
    dict[str, int],
    dict[str, Any],
]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read evidence checkpoint: {type(exc).__name__}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format") != "bian-evidence"
        or payload.get("format_version") != EVIDENCE_FORMAT_VERSION
    ):
        raise ValueError("unsupported evidence checkpoint version")
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list):
        raise ValueError("checkpoint evidence must be a list")
    metadata = payload.get("metadata", {})
    source_coverage = payload.get("source_coverage", {})
    if not isinstance(metadata, dict) or not isinstance(source_coverage, dict):
        raise ValueError("invalid evidence checkpoint metadata")
    bounds = (
        _required_time(payload.get("observation_start"), "observation_start"),
        _required_time(payload.get("observation_end"), "observation_end"),
    )
    if bounds[1] < bounds[0]:
        raise ValueError("evidence checkpoint observation bounds are reversed")
    return (
        tuple(_deserialize_evidence(item) for item in raw_evidence),
        bounds,
        {str(source): int(count) for source, count in source_coverage.items()},
        metadata,
    )

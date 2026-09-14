"""Portable event checkpoint between CPU preprocessing and LLM inference."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from .preprocessing.observations import AnomalyEvidence, DetectedEvent, parse_time


FORMAT_VERSION = 1


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
    }


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
        evidence = tuple(
            AnomalyEvidence(
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
            )
            for item in raw["evidence"]
        )
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

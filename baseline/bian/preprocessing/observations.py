"""Canonical observation contracts shared by the hybrid diagnosis pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Literal


AnomalyDirection = Literal["high", "low", "both", "state"]
DimensionTuple = tuple[tuple[str, str], ...]


def parse_time(value: str | None) -> datetime | None:
    """Parse an observation timestamp and normalize it to UTC."""
    if value is None:
        return None
    clean = value.strip().strip('"')
    if not clean:
        return None
    clean = clean.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def city_from_path(path: Path, aliases: dict[str, str]) -> str | None:
    """Resolve a public city ID from directory components, not file contents."""
    parts = tuple(part.lower() for part in path.parts)
    for alias, city in aliases.items():
        clean_alias = alias.lower()
        if any(
            part == clean_alias
            or part.startswith(clean_alias + "_")
            or part.startswith(clean_alias + "-")
            for part in parts
        ):
            return city
    return None


def normalize_node_id(
    raw: str | None,
    city: str | None,
    valid_roles: tuple[str, ...] | list[str],
) -> str | None:
    """Convert a source-specific hostname or role into a public element ID."""
    if raw is None or city is None:
        return None
    clean = raw.strip().strip('"').lower().replace("_", "-")
    if not clean:
        return None
    for role in sorted(valid_roles, key=len, reverse=True):
        normalized_role = role.lower()
        if normalized_role in clean:
            return f"{city}-{normalized_role}"
    return None


def _canonical_dimensions(values: DimensionTuple) -> DimensionTuple:
    return tuple(sorted((str(key), str(value)) for key, value in values if str(value)))


def _validate_timestamp(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone information")


@dataclass(frozen=True, slots=True)
class NumericObservation:
    timestamp: datetime
    source: str
    node_id: str | None
    related_node_ids: tuple[str, ...]
    metric: str
    value: float
    dimensions: DimensionTuple
    direction: AnomalyDirection = "both"

    def __post_init__(self) -> None:
        _validate_timestamp(self.timestamp)
        if not self.source or not self.metric:
            raise ValueError("source and metric are required")
        if not math.isfinite(self.value):
            raise ValueError("numeric observation value must be finite")
        if self.direction not in {"high", "low", "both", "state"}:
            raise ValueError("invalid anomaly direction")
        object.__setattr__(self, "dimensions", _canonical_dimensions(self.dimensions))
        object.__setattr__(self, "related_node_ids", tuple(dict.fromkeys(self.related_node_ids)))

    @property
    def series_key(self) -> tuple[str, str | None, str, DimensionTuple]:
        return self.source, self.node_id, self.metric, self.dimensions


@dataclass(frozen=True, slots=True)
class TextEvent:
    timestamp: datetime
    source: str
    node_id: str | None
    severity: str
    program: str
    event_family: str
    message: str
    dimensions: DimensionTuple = ()

    def __post_init__(self) -> None:
        _validate_timestamp(self.timestamp)
        if not self.source or not self.event_family:
            raise ValueError("source and event family are required")
        object.__setattr__(self, "message", " ".join(self.message.split())[:240])
        object.__setattr__(self, "dimensions", _canonical_dimensions(self.dimensions))


@dataclass(frozen=True, slots=True)
class AnomalyEvidence:
    timestamp: datetime
    source: str
    node_id: str | None
    related_node_ids: tuple[str, ...]
    metric: str
    value: float
    baseline: float
    score: float
    direction: AnomalyDirection
    dimensions: DimensionTuple
    summary: str | None = None

    def __post_init__(self) -> None:
        _validate_timestamp(self.timestamp)
        for name, value in (("value", self.value), ("baseline", self.baseline), ("score", self.score)):
            if not math.isfinite(value):
                raise ValueError(f"evidence {name} must be finite")
        if not 0.0 <= self.score <= 25.0:
            raise ValueError("evidence score must be in [0, 25]")
        if self.direction not in {"high", "low", "both", "state"}:
            raise ValueError("invalid anomaly direction")
        object.__setattr__(self, "dimensions", _canonical_dimensions(self.dimensions))
        object.__setattr__(self, "related_node_ids", tuple(dict.fromkeys(self.related_node_ids)))
        if self.summary is not None:
            object.__setattr__(self, "summary", " ".join(self.summary.split())[:240])


@dataclass(frozen=True, slots=True)
class DetectedEvent:
    start: datetime
    end: datetime
    peak_time: datetime
    confidence: float
    evidence: tuple[AnomalyEvidence, ...]
    source_counts: dict[str, int]

    def __post_init__(self) -> None:
        _validate_timestamp(self.start)
        _validate_timestamp(self.end)
        _validate_timestamp(self.peak_time)
        if self.end <= self.start:
            raise ValueError("event end must be later than start")
        if not self.start <= self.peak_time <= self.end:
            raise ValueError("event peak must lie inside the interval")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("event confidence must be in [0, 1]")
        if not self.evidence:
            raise ValueError("event must contain evidence")


@dataclass(slots=True)
class ParseStats:
    files_by_source: dict[str, int] = field(default_factory=dict)
    rows_by_source: dict[str, int] = field(default_factory=dict)
    bad_rows_by_source: dict[str, int] = field(default_factory=dict)
    invalid_numeric_values_by_source: dict[str, int] = field(default_factory=dict)
    unmapped_entities_by_source: dict[str, int] = field(default_factory=dict)
    out_of_order_rows_by_source: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    timestamp_ranges_by_source: dict[str, dict[str, str]] = field(default_factory=dict)
    file_audit: dict[str, dict[str, object]] = field(default_factory=dict)
    current_file: str | None = None

    @staticmethod
    def _increment(values: dict[str, int], source: str) -> None:
        values[source] = values.get(source, 0) + 1

    def record_file(self, source: str, path: Path | None = None) -> None:
        self._increment(self.files_by_source, source)
        self.current_file = str(path) if path is not None else None
        if self.current_file is not None:
            self.file_audit[self.current_file] = {
                "source": source,
                "rows": 0,
                "bad_rows": 0,
                "invalid_numeric_values": 0,
                "unmapped_entities": 0,
                "out_of_order_rows": 0,
                "timestamps": 0,
                "first_timestamp": None,
                "last_timestamp": None,
            }

    def record_row(self, source: str) -> None:
        self._increment(self.rows_by_source, source)
        audit = self._current_audit()
        if audit is not None:
            audit["rows"] = int(audit["rows"]) + 1

    def record_bad_row(self, source: str) -> None:
        self._increment(self.bad_rows_by_source, source)
        audit = self._current_audit()
        if audit is not None:
            audit["bad_rows"] = int(audit["bad_rows"]) + 1

    def record_invalid_numeric(self, source: str) -> None:
        self._increment(self.invalid_numeric_values_by_source, source)
        audit = self._current_audit()
        if audit is not None:
            audit["invalid_numeric_values"] = int(audit["invalid_numeric_values"]) + 1

    def record_unmapped_entity(self, source: str) -> None:
        self._increment(self.unmapped_entities_by_source, source)
        audit = self._current_audit()
        if audit is not None:
            audit["unmapped_entities"] = int(audit["unmapped_entities"]) + 1

    def record_out_of_order_row(self, source: str) -> None:
        self._increment(self.out_of_order_rows_by_source, source)
        audit = self._current_audit()
        if audit is not None:
            audit["out_of_order_rows"] = int(audit["out_of_order_rows"]) + 1

    def record_timestamp(self, source: str, value: datetime) -> None:
        rendered = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        bounds = self.timestamp_ranges_by_source.setdefault(
            source, {"first": rendered, "last": rendered}
        )
        if rendered < bounds["first"]:
            bounds["first"] = rendered
        if rendered > bounds["last"]:
            bounds["last"] = rendered
        audit = self._current_audit()
        if audit is not None:
            audit["timestamps"] = int(audit["timestamps"]) + 1
            if audit["first_timestamp"] is None or rendered < str(audit["first_timestamp"]):
                audit["first_timestamp"] = rendered
            if audit["last_timestamp"] is None or rendered > str(audit["last_timestamp"]):
                audit["last_timestamp"] = rendered

    def record_schema_issue(self, source: str, message: str) -> None:
        self.warn(message)
        audit = self._current_audit()
        if audit is not None:
            audit["schema_issue"] = message

    def _current_audit(self) -> dict[str, object] | None:
        if self.current_file is None:
            return None
        return self.file_audit.get(self.current_file)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

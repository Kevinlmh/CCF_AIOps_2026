"""Portable raw-evidence contracts, independent of features and model outputs."""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative_path: str
    source: str


@dataclass(frozen=True)
class RawRecord:
    batch: str
    record_id: str
    source: str
    source_file: str
    record_index: int
    line_start: int
    line_end: int
    timestamp: str | None
    city: str | None
    node_id: str | None
    dimensions: dict[str, str]
    values: dict[str, float]
    text: dict[str, str]
    raw: dict[str, str | None]
    extra_cells: tuple[str, ...]
    quality_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

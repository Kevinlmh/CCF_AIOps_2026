"""Stable public contracts shared by v2 data, model, and output layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


Modality = Literal["node", "edge", "log"]
Aggregation = Literal["last", "min", "max", "sum", "mean"]


@dataclass(frozen=True, order=True, slots=True)
class EdgeKey:
    """A deterministic directed relation used by the edge tensor."""

    source: str
    target: str
    relation: str

    def __post_init__(self) -> None:
        if not self.source or not self.target or not self.relation:
            raise ValueError("edge source, target, and relation are required")

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
        }

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> "EdgeKey":
        return cls(value["source"], value["target"], value["relation"])


@dataclass(frozen=True, slots=True)
class ProjectedFeature:
    """One observation projected into a minute/entity/feature cell."""

    minute: datetime
    modality: Modality
    entity: str | EdgeKey
    feature: str
    value: float
    aggregation: Aggregation

    def __post_init__(self) -> None:
        if self.minute.second or self.minute.microsecond:
            raise ValueError("projected feature timestamp must be minute-aligned")
        if self.modality == "edge" and not isinstance(self.entity, EdgeKey):
            raise ValueError("edge features require an EdgeKey entity")
        if self.modality != "edge" and not isinstance(self.entity, str):
            raise ValueError("node and log features require a node ID")
        if not self.feature:
            raise ValueError("feature name is required")

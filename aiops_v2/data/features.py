"""Stable feature-name indexing by tensor modality."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FeatureRegistry:
    """Collect feature names and freeze them into deterministic indices."""

    MODALITIES = ("node", "edge", "log")

    def __init__(self) -> None:
        self._pending = {modality: set() for modality in self.MODALITIES}
        self._names = {modality: () for modality in self.MODALITIES}
        self._indices: dict[str, dict[str, int]] = {
            modality: {} for modality in self.MODALITIES
        }
        self._frozen = False

    def add(self, modality: str, name: str) -> None:
        if modality not in self._pending:
            raise ValueError(f"unknown modality: {modality}")
        if self._frozen:
            raise RuntimeError("feature registry is frozen")
        if not name:
            raise ValueError("feature name is required")
        self._pending[modality].add(name)

    def freeze(self) -> None:
        if self._frozen:
            return
        for modality in self.MODALITIES:
            names = tuple(sorted(self._pending[modality]))
            self._names[modality] = names
            self._indices[modality] = {name: index for index, name in enumerate(names)}
        self._frozen = True

    def names(self, modality: str) -> tuple[str, ...]:
        if not self._frozen:
            raise RuntimeError("feature registry must be frozen before reading indices")
        return self._names[modality]

    def index(self, modality: str, name: str) -> int:
        if not self._frozen:
            raise RuntimeError("feature registry must be frozen before reading indices")
        try:
            return self._indices[modality][name]
        except KeyError as exc:
            raise KeyError(f"unknown {modality} feature: {name}") from exc

    def to_dict(self) -> dict[str, list[str]]:
        if not self._frozen:
            raise RuntimeError("feature registry must be frozen before serialization")
        return {modality: list(self._names[modality]) for modality in self.MODALITIES}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FeatureRegistry":
        result = cls()
        for modality in cls.MODALITIES:
            for name in value.get(modality, []):
                result.add(modality, str(name))
        result.freeze()
        return result

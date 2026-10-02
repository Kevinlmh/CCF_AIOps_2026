"""Compatibility entrypoint for the canonical v3 feature-store builder."""

from __future__ import annotations

from pathlib import Path

from .data.build import build_raw_feature_store


def build_sample_store(raw_root: Path, destination: Path, *, profile: str = "stage1") -> Path:
    """Build a full v2-style, v3-audited store for one case or raw batch."""
    return build_raw_feature_store(raw_root, destination, profile=profile)

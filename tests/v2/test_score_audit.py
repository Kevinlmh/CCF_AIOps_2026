from __future__ import annotations

from datetime import datetime, timezone
import importlib

import numpy as np
import pytest

from aiops_v2.contracts import EdgeKey
from aiops_v2.training.inference import TimelineScores


START = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)


def test_high_score_audit_names_observed_family_entity_and_error_component() -> None:
    audit = importlib.import_module("aiops_v2.score_audit")
    timeline = TimelineScores(
        family=np.zeros((3, 3), dtype=np.float32),
        node=np.asarray([[1, 2], [7, 3], [0, 0]], dtype=np.float32),
        edge=np.asarray([[9], [1], [0]], dtype=np.float32),
        log=np.zeros((3, 2), dtype=np.float32),
        coverage=np.ones(3, dtype=np.int32),
        family_observed=np.asarray([
            [True, True, False], [True, False, False], [True, True, False],
        ]),
        components={
            "edge_level": np.asarray([[2], [0], [0]], dtype=np.float32),
            "edge_reconstruction": np.asarray([[8], [0], [0]], dtype=np.float32),
            "edge_forecast": np.asarray([[1], [0], [0]], dtype=np.float32),
            "node_level": np.asarray([[0, 0], [5, 0], [0, 0]], dtype=np.float32),
            "node_reconstruction": np.asarray([[0, 0], [1, 0], [0, 0]], dtype=np.float32),
            "node_forecast": np.asarray([[0, 0], [2, 0], [0, 0]], dtype=np.float32),
        },
    )
    standardized = np.asarray([[2, 5, 0], [4, 100, 0], [0, 0, 0]], dtype=np.float32)
    probabilities = np.asarray([0.9, 0.87, 0.2], dtype=np.float32)

    result = audit.build_score_audit(
        probabilities, timeline, standardized, START,
        nodes=("beida-br-1", "beida-br-2"),
        edges=(EdgeKey("beida-br-1", "service-group:beida:web", "traffic"),),
        threshold=0.8,
    )

    assert result["summary"]["high_minute_count"] == 2
    assert result["summary"]["dominant_family_counts"] == {"edge": 1, "node": 1}
    first, second = result["high_minutes"]
    assert first["timestamp"] == "2026-08-19T04:00:00Z"
    assert first["dominant_family"] == "edge"
    assert first["top_entity"] == {
        "source": "beida-br-1", "target": "service-group:beida:web", "relation": "traffic"
    }
    assert first["dominant_component"] == "reconstruction"
    assert first["weighted_components"] == {
        "level": pytest.approx(0.9),
        "reconstruction": pytest.approx(2.0),
        "forecast": pytest.approx(0.3),
    }
    assert second["dominant_family"] == "node"
    assert second["top_entity"] == "beida-br-1"
    assert second["family_z_scores"]["edge"] is None
    assert second["dominant_component"] == "level"


def test_high_score_audit_rejects_misaligned_probabilities() -> None:
    audit = importlib.import_module("aiops_v2.score_audit")
    timeline = TimelineScores(
        family=np.zeros((2, 3), dtype=np.float32),
        node=np.zeros((2, 1), dtype=np.float32),
        edge=np.zeros((2, 0), dtype=np.float32),
        log=np.zeros((2, 1), dtype=np.float32),
        coverage=np.ones(2, dtype=np.int32),
    )

    with pytest.raises(ValueError, match="matching minute counts"):
        audit.build_score_audit(
            np.asarray([0.9]), timeline, np.zeros((2, 3)), START,
            nodes=("beida-br-1",), edges=(),
        )


def test_edge_audit_reports_only_edges_eligible_to_trigger() -> None:
    audit = importlib.import_module("aiops_v2.score_audit")
    timeline = TimelineScores(
        family=np.zeros((1, 3), dtype=np.float32),
        node=np.zeros((1, 1), dtype=np.float32),
        edge=np.asarray([[100.0, 2.0]], dtype=np.float32),
        log=np.zeros((1, 1), dtype=np.float32),
        coverage=np.ones(1, dtype=np.int32),
        family_observed=np.asarray([[False, True, False]]),
    )

    result = audit.build_score_audit(
        np.asarray([0.9]), timeline, np.asarray([[0.0, 5.0, 0.0]]), START,
        nodes=("beida-br-1",),
        edges=(
            EdgeKey("beida-br-1", "interface:beida-br-1:ens4", "netflow"),
            EdgeKey("beida-br-1", "service-group:beida:web", "traffic"),
        ),
        edge_trigger_eligibility=np.asarray([[False, True]]),
    )

    assert result["high_minutes"][0]["top_entity"]["relation"] == "traffic"

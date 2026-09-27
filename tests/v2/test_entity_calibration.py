from __future__ import annotations

import numpy as np
import pytest

from aiops_v2.training.entity_calibration import (
    EntityScoreCalibrator,
    calibrated_family_scores,
    pool_entity_scores,
)


def test_entity_score_calibration_normalizes_each_entity_and_respects_missingness() -> None:
    scores = np.array(
        [[1.0, 100.0], [1.0, 100.0], [1.0, 100.0], [10.0, 100.0]],
        dtype=np.float32,
    )
    observed = np.ones_like(scores, dtype=bool)

    calibrator = EntityScoreCalibrator.fit(scores[:3], observed[:3])
    calibrated = calibrator.transform(scores, observed)

    assert calibrated[3, 0] > calibrated[3, 1]
    assert np.all(calibrated[:3] < 1e-5)

    observed[3, 0] = False
    masked = calibrator.transform(scores, observed)
    assert masked[3, 0] == 0


def test_entity_score_calibration_rejects_shape_mismatch() -> None:
    calibrator = EntityScoreCalibrator.fit(np.ones((4, 2)), np.ones((4, 2), bool))

    with pytest.raises(ValueError, match="mask shape"):
        calibrator.transform(np.ones((4, 2)), np.ones((4, 1), bool))


def test_entity_calibration_handles_modalities_without_entities() -> None:
    scores = np.zeros((5, 0), dtype=np.float32)
    mask = np.zeros_like(scores, dtype=bool)
    calibrator = EntityScoreCalibrator.fit(scores, mask)

    calibrated, observed, entities = pool_entity_scores(scores, mask, calibrator)

    assert calibrated.tolist() == [0.0] * 5
    assert not observed.any()
    assert entities.shape == (5, 0)


def test_entity_score_calibrator_round_trip() -> None:
    values = np.array([[0.0, 2.0], [1.0, 2.0], [2.0, 2.0], [8.0, 2.0]])
    mask = np.ones_like(values, dtype=bool)
    calibrator = EntityScoreCalibrator.fit(values, mask)

    restored = EntityScoreCalibrator.from_dict(calibrator.to_dict())

    assert np.allclose(restored.transform(values, mask), calibrator.transform(values, mask))


def test_family_pool_never_triggers_netflow_and_gates_traffic_by_service_quality() -> None:
    base = np.array([[1.0, 1.0], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0]])
    mask = np.ones_like(base, dtype=bool)
    calibrators = {
        name: EntityScoreCalibrator.fit(base, mask)
        for name in ("node", "edge", "log")
    }
    edge_scores = np.array([[100.0, 1.0], [100.0, 1.0], [100.0, 1.0], [100.0, 10.0]])
    family, observed = calibrated_family_scores(
        node_scores=base,
        edge_scores=edge_scores,
        log_scores=base,
        node_observed=mask,
        edge_observed=mask,
        log_observed=mask,
        calibrators=calibrators,
        traffic_edges=np.array([False, True]),
        service_gate=np.array([[False, False]] * 3 + [[False, True]]),
    )

    assert family[3, 1] > 0
    assert family[:3, 1].tolist() == [0.0, 0.0, 0.0]
    assert observed[:, 1].tolist() == [True, True, True, True]


def test_node_neural_spike_requires_same_entity_semantic_support() -> None:
    base = np.ones((4, 2), dtype=np.float32)
    mask = np.ones_like(base, dtype=bool)
    calibrators = {
        "node": EntityScoreCalibrator.fit(base, mask),
        "edge": EntityScoreCalibrator.fit(np.zeros((4, 0)), np.zeros((4, 0), bool)),
        "log": EntityScoreCalibrator.fit(base, mask),
    }
    neural_node = base.copy()
    neural_node[-1, 1] = 100.0
    family, observed = calibrated_family_scores(
        node_scores=neural_node,
        edge_scores=np.zeros((4, 0)),
        log_scores=np.zeros((4, 2)),
        node_observed=mask,
        edge_observed=np.zeros((4, 0), dtype=bool),
        log_observed=mask,
        calibrators=calibrators,
        traffic_edges=np.zeros(0, dtype=bool),
        service_gate=np.zeros((4, 0), dtype=bool),
        node_gate=np.asarray([[True, False]] * 3 + [[True, False]]),
    )

    assert family[-1, 0] == pytest.approx(0.0, abs=1e-5)
    assert observed[-1, 0]

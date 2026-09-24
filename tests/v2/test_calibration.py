from __future__ import annotations

import numpy as np

from aiops_v2.training.calibration import AnomalyCalibrator


def test_calibrator_maps_normal_minutes_low_and_joint_spike_high() -> None:
    normal = np.array(
        [[1.0, 0.8, 0.4], [1.1, 0.7, 0.5], [0.9, 0.9, 0.3], [1.0, 0.8, 0.4]],
        dtype=np.float32,
    )
    calibrator = AnomalyCalibrator.fit(normal)
    values = np.vstack((normal, np.array([[8.0, 6.0, 4.0]], dtype=np.float32)))

    probabilities = calibrator.transform(values)

    assert float(probabilities[:4].max()) < 0.5
    assert float(probabilities[-1]) > 0.95


def test_calibrator_accepts_three_minute_smoke_sequence() -> None:
    values = np.array(
        [[1.0, 0.8, 0.4], [1.1, 0.7, 0.5], [0.9, 0.9, 0.3]],
        dtype=np.float32,
    )

    calibrator = AnomalyCalibrator.fit(values)

    assert calibrator.center.shape == (3,)


def test_calibrator_requires_multiple_finite_three_family_rows() -> None:
    for invalid in (
        np.ones((2, 3), dtype=np.float32),
        np.ones((5, 2), dtype=np.float32),
        np.array([[1.0, 2.0, np.nan]] * 5),
    ):
        try:
            AnomalyCalibrator.fit(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid calibration data was accepted")


def test_calibrator_round_trip_preserves_probabilities() -> None:
    values = np.array(
        [[1.0, 2.0, 0.2], [1.2, 2.2, 0.3], [0.9, 1.8, 0.1], [1.1, 2.1, 0.2]],
        dtype=np.float32,
    )
    original = AnomalyCalibrator.fit(values)

    restored = AnomalyCalibrator.from_dict(original.to_dict())

    assert np.allclose(original.transform(values), restored.transform(values))


def test_calibrator_excludes_unobserved_family_values_and_reports_counts() -> None:
    values = np.tile(np.array([1.0, 0.8, 1_000_000.0]), (12, 1))
    values[-1, 0] = 8.0
    observed = np.ones_like(values, dtype=bool)
    observed[:, 2] = False

    calibrator = AnomalyCalibrator.fit(values, observed)
    first = calibrator.transform(values, observed)
    changed = values.copy()
    changed[:, 2] = 1e20
    second = calibrator.transform(changed, observed)

    assert np.allclose(first, second)
    assert calibrator.summary.valid_minute_count == (12, 12, 0)
    assert calibrator.summary.reference_minute_count == (12, 12, 0)
    assert np.isfinite(first).all()


def test_calibrator_returns_zero_probability_when_no_family_is_observed() -> None:
    values = np.ones((5, 3), dtype=np.float32)
    calibrator = AnomalyCalibrator.fit(values)

    probabilities = calibrator.transform(values, np.zeros_like(values, dtype=bool))

    assert probabilities.tolist() == [0.0] * 5


def test_calibrator_exposes_standardized_family_evidence_with_missing_mask() -> None:
    values = np.array([[1.0, 2.0, 3.0]] * 6, dtype=np.float32)
    observed = np.ones_like(values, dtype=bool)
    observed[:, 1] = False
    calibrator = AnomalyCalibrator.fit(values, observed)

    evidence = calibrator.standardize(values, observed)

    assert evidence.shape == values.shape
    assert np.all(evidence[:, 1] == 0.0)
    assert np.isfinite(evidence).all()

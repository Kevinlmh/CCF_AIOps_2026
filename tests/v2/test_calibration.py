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

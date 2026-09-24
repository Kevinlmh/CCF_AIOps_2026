from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from aiops_v2.events.decoder import DecoderConfig, DecoderEvidence, decode_events


ORIGIN = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)


def test_decoder_returns_one_global_event_for_one_probability_plateau() -> None:
    probabilities = np.full(30, 0.05, dtype=np.float32)
    probabilities[8:16] = 0.92

    events = decode_events(probabilities, ORIGIN)

    assert len(events) == 1
    assert events[0].start_index == 8
    assert events[0].end_index == 15
    assert events[0].duration_minutes == 8
    assert events[0].start_time == datetime(2026, 8, 19, 4, 8, tzinfo=timezone.utc)
    assert events[0].end_time == datetime(2026, 8, 19, 4, 16, tzinfo=timezone.utc)


def test_decoder_keeps_supported_one_minute_fault_but_rejects_weak_spike() -> None:
    probabilities = np.full(20, 0.05, dtype=np.float32)
    probabilities[4] = 0.70
    probabilities[12] = 0.99

    events = decode_events(probabilities, ORIGIN)

    assert [(event.start_index, event.end_index) for event in events] == [(12, 12)]


def test_decoder_can_keep_two_strong_nearby_events_despite_soft_gap_prior() -> None:
    probabilities = np.full(40, 0.05, dtype=np.float32)
    probabilities[4:9] = 0.98
    probabilities[14:19] = 0.97

    events = decode_events(
        probabilities,
        ORIGIN,
        DecoderConfig(close_event_penalty=0.04),
    )

    assert [(event.start_index, event.end_index) for event in events] == [(4, 8), (14, 18)]


def test_decoder_never_emits_event_longer_than_thirty_minutes() -> None:
    probabilities = np.full(60, 0.05, dtype=np.float32)
    probabilities[5:45] = 0.95

    events = decode_events(probabilities, ORIGIN)

    assert len(events) == 1
    assert events[0].duration_minutes == 30


def test_decoder_does_not_extend_high_confidence_interval_for_weak_tail() -> None:
    probabilities = np.full(40, 0.05, dtype=np.float64)
    probabilities[8:13] = 0.95
    probabilities[13:18] = 0.45

    events = decode_events(probabilities, ORIGIN)

    assert [(event.start_index, event.end_index) for event in events] == [(8, 12)]


def test_decoder_preserves_exact_thirty_minute_event_boundaries() -> None:
    probabilities = np.full(45, 0.05, dtype=np.float32)
    probabilities[5:35] = 0.95

    events = decode_events(probabilities, ORIGIN)

    assert [(event.start_index, event.end_index) for event in events] == [(5, 34)]


def test_decoder_audit_does_not_count_unobserved_family_as_support() -> None:
    probabilities = np.full(25, 0.05, dtype=np.float64)
    probabilities[8:13] = 0.95
    family_z = np.zeros((25, 3), dtype=np.float32)
    family_z[8:13, 0] = 4.0
    family_z[:, 1] = 100.0
    family_observed = np.zeros((25, 3), dtype=bool)
    family_observed[:, 0] = True
    evidence = DecoderEvidence(
        family_z_scores=family_z,
        family_observed=family_observed,
        node=np.zeros((25, 2), dtype=np.float32),
        edge=np.zeros((25, 0), dtype=np.float32),
        log=np.zeros((25, 2), dtype=np.float32),
    )

    events = decode_events(probabilities, ORIGIN, evidence=evidence)

    assert len(events) == 1
    assert events[0].evidence_scores["family_support"] == 0.0
    assert events[0].evidence_scores["node_peak"] == 0.0


def test_decoder_does_not_infer_recovery_beyond_observed_timeline() -> None:
    probabilities = np.full(12, 0.05, dtype=np.float64)
    probabilities[-2:] = 0.95

    events = decode_events(probabilities, ORIGIN)

    assert len(events) == 1
    assert events[0].end_index == len(probabilities) - 1
    assert events[0].evidence_scores["recovery_drop"] == 0.0
    assert events[0].evidence_scores["recovery_term"] == 0.0
    assert events[0].evidence_scores["boundary_term"] == pytest.approx(0.45)


def test_decoder_does_not_infer_onset_before_observed_timeline() -> None:
    probabilities = np.array([0.95, 0.95, 0.05], dtype=np.float64)

    events = decode_events(probabilities, ORIGIN)

    assert len(events) == 1
    assert events[0].start_index == 0
    assert events[0].evidence_scores["onset"] == 0.0
    assert events[0].evidence_scores["boundary_term"] == pytest.approx(0.45)


def test_decoder_rejects_invalid_probability_values() -> None:
    with pytest.raises(ValueError, match="finite probabilities"):
        decode_events(np.array([0.1, np.nan]), ORIGIN)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        decode_events(np.array([0.1, 1.2]), ORIGIN)

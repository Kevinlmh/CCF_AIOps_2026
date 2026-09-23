from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from aiops_v2.events.decoder import DecoderConfig, decode_events


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


def test_decoder_rejects_invalid_probability_values() -> None:
    with pytest.raises(ValueError, match="finite probabilities"):
        decode_events(np.array([0.1, np.nan]), ORIGIN)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        decode_events(np.array([0.1, 1.2]), ORIGIN)

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from types import SimpleNamespace

from aiops_v2.detection.direct_evidence import DirectEvidence
from aiops_v2.events.city_decoder import decode_city_events
from aiops_v2.events.decoder import (
    DecodedEvent,
    DecoderConfig,
    DecoderEvidence,
    decode_events,
    reconcile_events,
)


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


def test_city_decoder_preserves_simultaneous_faults_in_different_cities() -> None:
    probabilities = np.full((30, 2), 0.05, dtype=np.float32)
    probabilities[8:15, 0] = 0.96
    probabilities[8:15, 1] = 0.95
    categories = np.zeros((30, 2, 1), dtype=np.float32)
    observed = np.ones((30, 2), dtype=bool)
    direct = DirectEvidence(
        category_names=("cpu_pressure",),
        category_scores=categories,
        node_probability=probabilities,
        global_probability=np.max(probabilities, axis=1),
        observed=observed,
        feature_audit={},
        edge_symptom_scores=np.zeros((30, 0), dtype=np.float32),
        service_probability=np.zeros(30, dtype=np.float32),
    )
    nodes = ("chengdu-service-vm-1", "wuhan-service-vm-1")

    events = decode_city_events(direct, nodes, (), ORIGIN)

    assert len(events) == 2
    assert {(event.city_id, event.start_index, event.end_index) for event in events} == {
        ("chengdu", 8, 14),
        ("wuhan", 8, 14),
    }
    assert all(event.root_city_scoped for event in events)


def test_reconciliation_keeps_strong_incident_over_two_overlapping_weak_candidates() -> None:
    strong = DecodedEvent(
        8, 18, 10, 0.95, 4.0,
        ORIGIN + timedelta(minutes=8), ORIGIN + timedelta(minutes=19),
        city_id="chengdu",
    )
    early = DecodedEvent(
        8, 12, 9, 0.9, 2.4,
        ORIGIN + timedelta(minutes=8), ORIGIN + timedelta(minutes=13),
        city_id="wuhan", root_city_scoped=True,
    )
    late = DecodedEvent(
        13, 18, 14, 0.9, 2.4,
        ORIGIN + timedelta(minutes=13), ORIGIN + timedelta(minutes=19),
        city_id="shanghai",
    )

    assert reconcile_events((early, late, strong)) == (strong,)


def test_reconciliation_tiebreaks_direct_evidence_and_keeps_adjacent_event() -> None:
    service = DecodedEvent(
        8, 12, 9, 0.9, 2.0,
        ORIGIN + timedelta(minutes=8), ORIGIN + timedelta(minutes=13),
        city_id="chengdu",
    )
    direct = DecodedEvent(
        8, 12, 9, 0.9, 2.0,
        ORIGIN + timedelta(minutes=8), ORIGIN + timedelta(minutes=13),
        city_id="wuhan", root_city_scoped=True,
    )
    adjacent = DecodedEvent(
        13, 15, 14, 0.85, 1.0,
        ORIGIN + timedelta(minutes=13), ORIGIN + timedelta(minutes=16),
        city_id="shanghai",
    )

    assert reconcile_events((service, adjacent, direct)) == (direct, adjacent)


def test_city_decoder_assigns_service_symptom_to_target_not_probe_city() -> None:
    node_probability = np.full((30, 2), 0.05, dtype=np.float32)
    categories = np.zeros((30, 2, 1), dtype=np.float32)
    edge_scores = np.zeros((30, 1), dtype=np.float32)
    edge_scores[8:14, 0] = 8.0
    direct = DirectEvidence(
        category_names=("cpu_pressure",),
        category_scores=categories,
        node_probability=node_probability,
        global_probability=np.full(30, 0.9, dtype=np.float32),
        observed=np.ones_like(node_probability, dtype=bool),
        feature_audit={},
        edge_symptom_scores=edge_scores,
        service_probability=np.full(30, 0.9, dtype=np.float32),
    )
    nodes = ("chengdu-service-vm-1", "wuhan-service-vm-1")
    edges = (SimpleNamespace(relation="traffic", target="service-group:wuhan:web"),)

    events = decode_city_events(direct, nodes, edges, ORIGIN)

    assert len(events) == 1
    assert events[0].city_id == "wuhan"
    assert not events[0].root_city_scoped


def test_city_decoder_does_not_join_unrelated_adjacent_service_edges() -> None:
    node_probability = np.full((30, 1), 0.05, dtype=np.float32)
    categories = np.zeros((30, 1, 1), dtype=np.float32)
    edge_scores = np.zeros((30, 2), dtype=np.float32)
    edge_scores[10, 0] = 8.0
    edge_scores[11, 1] = 8.0
    direct = DirectEvidence(
        category_names=("cpu_pressure",),
        category_scores=categories,
        node_probability=node_probability,
        global_probability=np.full(30, 0.9, dtype=np.float32),
        observed=np.ones_like(node_probability, dtype=bool),
        feature_audit={},
        edge_symptom_scores=edge_scores,
        service_probability=np.full(30, 0.9, dtype=np.float32),
    )
    nodes = ("wuhan-service-vm-1",)
    edges = (
        SimpleNamespace(relation="traffic", target="service-group:wuhan:web"),
        SimpleNamespace(relation="traffic", target="service-group:wuhan:auth"),
    )

    events = decode_city_events(direct, nodes, edges, ORIGIN)

    assert events == ()

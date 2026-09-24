from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from aiops_challenge_2026.config import load_public_config
from aiops_v2.diagnosis_schema import ROOT_FEATURE_NAMES, EVENT_FEATURE_NAMES
from aiops_v2.data.feature_store import build_feature_store
from aiops_v2.events.decoder import DecodedEvent
from aiops_v2.models.heads import EventDiagnosisHeads
from aiops_v2.localization.ranking import (
    ROOT_FEATURE_NAMES,
    extract_candidate_evidence,
    rank_root_causes,
)
from aiops_v2.training.inference import TimelineScores
from baseline.bian.preprocessing.observations import NumericObservation


START = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)


def _traffic(source_city: str, target_city: str) -> NumericObservation:
    return NumericObservation(
        timestamp=START,
        source="traffic",
        node_id=f"{source_city}-traffic-vm",
        related_node_ids=(),
        metric="traffic.web.error_ratio",
        value=0.5,
        dimensions=(("flow_type", "web"), ("target_region", target_city)),
        direction="high",
    )


def _event() -> DecodedEvent:
    return DecodedEvent(0, 0, 0, 0.95, 2.0, START, START.replace(minute=1))


def _empty_scores(store, minutes: int = 1) -> TimelineScores:
    return TimelineScores(
        family=np.zeros((minutes, 3), dtype=np.float32),
        node=np.zeros((minutes, 80), dtype=np.float32),
        edge=np.zeros((minutes, len(store.entities.edges)), dtype=np.float32),
        log=np.zeros((minutes, 80), dtype=np.float32),
        coverage=np.ones(minutes, dtype=np.int32),
    )


def test_traffic_symptoms_rank_target_service_nodes_above_observers(tmp_path) -> None:
    store = build_feature_store(
        [_traffic("chengdu", "wuhan"), _traffic("xian", "wuhan")],
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    scores = _empty_scores(store)
    scores.edge[:] = 8.0

    ranking = rank_root_causes(_event(), scores, store)

    assert ranking.top5[:3] == (
        "wuhan-service-vm-1",
        "wuhan-service-vm-2",
        "wuhan-service-vm-3",
    )
    assert "chengdu-traffic-vm" not in ranking.top5[:3]
    assert ranking.explanations["wuhan-service-vm-1"]["traffic_target"] > 0


def test_direct_node_anomaly_is_ranked_first(tmp_path) -> None:
    observation = NumericObservation(
        timestamp=START,
        source="node",
        node_id="chengdu-br-1",
        related_node_ids=(),
        metric="node.cpu_usage",
        value=80.0,
        dimensions=(),
        direction="high",
    )
    store = build_feature_store(
        [observation],
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    scores = _empty_scores(store)
    scores.node[0, store.entities.node_index("chengdu-br-1")] = 10.0

    ranking = rank_root_causes(_event(), scores, store)

    assert ranking.top5[0] == "chengdu-br-1"
    assert ranking.explanations["chengdu-br-1"]["direct"] == 10.0


def test_root_ranking_always_returns_five_unique_public_nodes(tmp_path) -> None:
    store = build_feature_store(
        [_traffic("chengdu", "wuhan")],
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    ranking = rank_root_causes(_event(), _empty_scores(store), store)

    assert len(ranking.top5) == 5
    assert len(set(ranking.top5)) == 5
    assert set(ranking.top5) <= set(store.entities.nodes)


def test_direct_cross_city_root_beats_unrelated_high_traffic_symptom(tmp_path) -> None:
    direct_root = "guangzhou-service-vm-3"
    direct = NumericObservation(
        timestamp=START,
        source="node",
        node_id=direct_root,
        related_node_ids=(),
        metric="node.cpu_usage",
        value=80.0,
        dimensions=(),
        direction="high",
    )
    store = build_feature_store(
        [direct, _traffic("nanjing", "nanjing")],
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    scores = _empty_scores(store)
    scores.node[0, store.entities.node_index(direct_root)] = 10.0
    scores.edge[0, :] = 50.0

    ranking = rank_root_causes(_event(), scores, store)

    assert ranking.top5[0] == direct_root
    assert direct_root in ranking.top5
    assert "nanjing-service-vm-1" in ranking.top5


def test_candidate_evidence_has_fixed_public_node_and_feature_order(tmp_path) -> None:
    store = build_feature_store(
        [_traffic("chengdu", "wuhan")],
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    evidence = extract_candidate_evidence(_event(), _empty_scores(store), store)

    assert evidence.candidate_nodes == store.entities.nodes
    assert evidence.feature_names == ROOT_FEATURE_NAMES
    assert evidence.features.shape == (80, len(ROOT_FEATURE_NAMES))
    assert evidence.candidate_mask.tolist() == [True] * 80


def test_root_ranking_integrates_learned_scores_without_breaking_public_top5(tmp_path) -> None:
    store = build_feature_store(
        [_traffic("chengdu", "wuhan")],
        load_public_config("network_elements"),
        tmp_path / "store",
    )
    heads = EventDiagnosisHeads(
        len(ROOT_FEATURE_NAMES),
        len(EVENT_FEATURE_NAMES),
        len(load_public_config("fault_taxonomy")["fault_categories"]),
        hidden_size=8,
    )

    ranking = rank_root_causes(_event(), _empty_scores(store), store, diagnosis_heads=heads)

    assert len(ranking.top5) == 5
    assert len(set(ranking.top5)) == 5
    assert set(ranking.top5) <= set(store.entities.nodes)
    assert sum(ranking.scores.values()) == pytest.approx(1.0)
    assert "learned_root_probability" in ranking.explanations[ranking.top5[0]]

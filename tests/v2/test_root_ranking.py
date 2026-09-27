from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def test_duplicate_target_city_symptoms_do_not_outvote_direct_device_evidence(tmp_path) -> None:
    root = "guangzhou-br-1"
    direct = NumericObservation(
        timestamp=START, source="node", node_id=root, related_node_ids=(),
        metric="node.cpu_usage", value=50.0, dimensions=(), direction="high",
    )
    store = build_feature_store(
        [direct, _traffic("chengdu", "wuhan"), _traffic("xian", "wuhan")],
        load_public_config("network_elements"), tmp_path / "store",
    )
    scores = _empty_scores(store)
    scores.node[0, store.entities.node_index(root)] = 1.0
    scores.edge[0, :] = 8.0

    ranking = rank_root_causes(_event(), scores, store)

    assert ranking.top5[0] == root
    assert ranking.explanations["wuhan-service-vm-1"]["traffic_target"] < 4.0


def test_same_observer_multiple_traffic_edges_do_not_outvote_direct_device(tmp_path) -> None:
    root = "guangzhou-br-1"
    observer = "chengdu-traffic-vm"
    direct = NumericObservation(
        timestamp=START, source="node", node_id=root, related_node_ids=(),
        metric="node.cpu_usage", value=50.0, dimensions=(), direction="high",
    )
    store = build_feature_store(
        [direct, _traffic("chengdu", "wuhan"), _traffic("chengdu", "beida"),
         _traffic("chengdu", "xian")],
        load_public_config("network_elements"), tmp_path / "store",
    )
    scores = _empty_scores(store)
    scores.node[0, store.entities.node_index(root)] = 1.0
    scores.edge[0, :] = 8.0

    ranking = rank_root_causes(_event(), scores, store)

    assert ranking.explanations[observer]["traffic_observer"] == pytest.approx(0.2)
    assert ranking.top5[0] == root


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


def test_chronic_netflow_shift_before_event_is_not_new_root_evidence(tmp_path) -> None:
    source = "guangzhou-br-1"
    root = "wuhan-service-vm-1"
    observations = [
        NumericObservation(START, "netflow", source, (), "netflow.protocol_byte_share", 0.8,
                           (("protocol", "6"), ("interface_id", "ens4")), "high"),
        *(
            NumericObservation(START + timedelta(minutes=minute), "node", root, (),
                               "node.cpu_usage", 2.0, (), "high")
            for minute in range(80)
        ),
    ]
    store = build_feature_store(observations, load_public_config("network_elements"), tmp_path / "store")
    scores = _empty_scores(store, minutes=80)
    scores.node[30:36, store.entities.node_index(root)] = 1.0
    scores.edge[20:36, store.entities.edge_index(store.entities.edges[0])] = 4.0
    event = DecodedEvent(30, 35, 32, 0.95, 2.0,
                         START + timedelta(minutes=30), START + timedelta(minutes=36))

    ranking = rank_root_causes(event, scores, store)

    assert ranking.top5[0] == root
    assert ranking.explanations[source]["netflow"] == 0.0


def test_new_netflow_shift_cannot_replace_root_without_direct_evidence(tmp_path) -> None:
    source = "guangzhou-br-1"
    root = "wuhan-service-vm-1"
    observations = [
        NumericObservation(START, "netflow", source, (), "netflow.protocol_byte_share", 0.8,
                           (("protocol", "6"), ("interface_id", "ens4")), "high"),
        *(
            NumericObservation(START + timedelta(minutes=minute), "node", root, (),
                               "node.cpu_usage", 2.0, (), "high")
            for minute in range(80)
        ),
    ]
    store = build_feature_store(observations, load_public_config("network_elements"), tmp_path / "store")
    scores = _empty_scores(store, minutes=80)
    scores.node[30:36, store.entities.node_index(root)] = 1.0
    scores.edge[30:36, 0] = 4.0
    event = DecodedEvent(30, 35, 32, 0.95, 2.0,
                         START + timedelta(minutes=30), START + timedelta(minutes=36))

    ranking = rank_root_causes(event, scores, store)

    assert ranking.explanations[source]["netflow"] > 0.0
    assert ranking.explanations[source]["effective_netflow"] == 0.0
    assert ranking.top5[0] == root


def test_new_direct_onset_outranks_chronic_pre_event_anomaly(tmp_path) -> None:
    chronic = "chengdu-br-1"
    root = "wuhan-br-1"
    observations = [
        NumericObservation(
            START + timedelta(minutes=minute), "node", node, (),
            "node.cpu_usage", 80.0, (), "high",
        )
        for minute in range(80)
        for node in (chronic, root)
    ]
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    scores = _empty_scores(store, minutes=80)
    scores.node[:36, store.entities.node_index(chronic)] = 4.0
    scores.node[27:36, store.entities.node_index(root)] = 3.0
    event = DecodedEvent(
        30, 35, 32, 0.95, 2.0,
        START + timedelta(minutes=30), START + timedelta(minutes=36),
    )

    ranking = rank_root_causes(event, scores, store)

    assert ranking.top5[0] == root
    assert ranking.explanations[root]["direct_pre"] > 0.0

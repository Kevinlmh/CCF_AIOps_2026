from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from aiops_challenge_2026.config import load_public_config
from aiops_v2.data.feature_store import build_feature_store
from aiops_v2.events.decoder import DecodedEvent
from aiops_v2.localization.ranking import rank_root_causes
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

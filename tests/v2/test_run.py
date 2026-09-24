from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import numpy as np

from aiops_challenge_2026.config import load_public_config
from aiops_challenge_2026.schema import validate_prediction
from aiops_v2.data.feature_store import build_feature_store
from aiops_v2.detection import score_direct_evidence
from aiops_v2.diagnosis_schema import EVENT_FEATURE_NAMES, ROOT_FEATURE_NAMES
from aiops_v2.events.decoder import DecodedEvent
from aiops_v2.models.heads import EventDiagnosisHeads
from aiops_v2.run import build_features, build_prediction_records, write_predictions
from aiops_v2.training.inference import TimelineScores
from baseline.bian.preprocessing.observations import NumericObservation


START = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)
FIXTURE = Path("tests/fixtures/multisource/case")


def test_build_features_command_reads_fixture_and_records_all_sources(tmp_path) -> None:
    store = build_features(FIXTURE, tmp_path / "store")

    assert set(store.manifest["source_counts"]) == {
        "node",
        "interface",
        "routing",
        "scrape",
        "traffic",
        "netflow",
        "frr",
    }
    assert store.manifest["observation_count"] > 0
    assert store.node_values.shape[1] == 80


def test_prediction_records_follow_official_schema_and_write_jsonl(tmp_path) -> None:
    observation = NumericObservation(
        timestamp=START,
        source="node",
        node_id="beida-br-1",
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
    node_scores = np.zeros((1, 80), dtype=np.float32)
    node_scores[0, store.entities.node_index("beida-br-1")] = 10.0
    timeline = TimelineScores(
        family=np.ones((1, 3), dtype=np.float32),
        node=node_scores,
        edge=np.zeros((1, 0), dtype=np.float32),
        log=np.zeros((1, 80), dtype=np.float32),
        coverage=np.ones(1, dtype=np.int32),
    )
    event = DecodedEvent(
        0,
        0,
        0,
        0.9,
        2.0,
        START,
        START + timedelta(minutes=1),
        evidence_scores={"interval": 0.7, "family_support": 0.5},
    )

    records, audit = build_prediction_records(
        (event,), timeline, store, load_public_config("fault_taxonomy")
    )
    output = tmp_path / "predictions.jsonl"
    write_predictions(records, output)

    validate_prediction(records[0])
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["root_cause_top5"][0]["network_element_id"] == "beida-br-1"
    assert audit[0]["prediction_id"] == "pred_000001"
    assert audit[0]["event"]["decoder_evidence"] == {
        "interval": 0.7,
        "family_support": 0.5,
    }


def test_prediction_records_wire_learned_heads_and_audit_candidate_categories(tmp_path) -> None:
    observation = NumericObservation(
        timestamp=START,
        source="node",
        node_id="beida-service-vm-1",
        related_node_ids=(),
        metric="node.cpu_usage",
        value=80.0,
        dimensions=(),
        direction="high",
    )
    store = build_feature_store(
        [observation], load_public_config("network_elements"), tmp_path / "store"
    )
    timeline = TimelineScores(
        family=np.ones((1, 3), dtype=np.float32),
        node=np.ones((1, 80), dtype=np.float32),
        edge=np.zeros((1, 0), dtype=np.float32),
        log=np.zeros((1, 80), dtype=np.float32),
        coverage=np.ones(1, dtype=np.int32),
    )
    event = DecodedEvent(0, 0, 0, 0.9, 2.0, START, START + timedelta(minutes=1))
    taxonomy = load_public_config("fault_taxonomy")
    heads = EventDiagnosisHeads(
        len(ROOT_FEATURE_NAMES),
        len(EVENT_FEATURE_NAMES),
        len(taxonomy["fault_categories"]),
        hidden_size=8,
    )

    records, audit = build_prediction_records(
        (event,), timeline, store, taxonomy, diagnosis_heads=heads
    )

    validate_prediction(records[0])
    assert "learned_root_probability" in audit[0]["root_scores"][0]["components"]
    assert len(audit[0]["classification"]["candidate_category_scores"]) == 80


def test_disk_space_direct_evidence_maps_to_official_taxonomy(tmp_path) -> None:
    observations = [
        NumericObservation(
            timestamp=START + timedelta(minutes=minute),
            source="node",
            node_id="beida-service-vm-1",
            related_node_ids=(),
            metric="node.filesystem_used_ratio",
            value=0.95 if 30 <= minute < 40 else 0.40,
            dimensions=(),
            direction="high",
        )
        for minute in range(70)
    ]
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    direct = score_direct_evidence(store)
    node = np.max(direct.category_scores, axis=2)
    timeline = TimelineScores(
        family=np.zeros((70, 3), dtype=np.float32),
        node=node,
        edge=np.zeros((70, 0), dtype=np.float32),
        log=np.zeros((70, 80), dtype=np.float32),
        coverage=np.ones(70, dtype=np.int32),
    )
    event = DecodedEvent(
        31, 38, 34, 0.95, 3.0,
        START + timedelta(minutes=31), START + timedelta(minutes=39),
    )
    records, audit = build_prediction_records(
        (event,), timeline, store, load_public_config("fault_taxonomy"),
        direct_evidence=direct,
    )
    assert records[0]["fault_category"] == {
        "major_category": "resource", "sub_category": "disk_space_low"
    }
    assert audit[0]["classification"]["direct_category_override"] == records[0]["fault_category"]

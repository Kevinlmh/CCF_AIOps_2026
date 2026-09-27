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
from aiops_v2.run import _direct_timeline, build_features, build_prediction_records, write_predictions
from aiops_v2.localization.ranking import rank_root_causes
from aiops_v2.training.inference import TimelineScores
from aiops_v2.training.trainer import TrainConfig, save_checkpoint, train_detector
from aiops_v2 import run
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


def test_build_features_rejects_incomplete_source_set(tmp_path) -> None:
    import pytest

    processed = tmp_path / "input" / "chengdu_window" / "processed"
    processed.mkdir(parents=True)
    (processed / "node_metrics.csv").write_text(
        "timestamp,node,cpu_usage\n2026-08-19T04:00:00Z,chengdu-service-vm-1,2\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing.*source"):
        build_features(tmp_path / "input", tmp_path / "store")


def test_build_features_rejects_incomplete_city_when_other_city_has_all_sources(tmp_path) -> None:
    import pytest

    source_names = (
        "node_metrics.csv", "interface_metrics.csv", "routing_metrics.csv",
        "scrape_health.csv", "traffic_flow_metrics.csv", "netflow.csv",
        "frr_syslog_events.csv",
    )
    complete = tmp_path / "input" / "beida_window" / "processed"
    incomplete = tmp_path / "input" / "chengdu_window" / "processed"
    complete.mkdir(parents=True)
    incomplete.mkdir(parents=True)
    for name in source_names:
        (complete / name).touch()
    (incomplete / "node_metrics.csv").touch()

    with pytest.raises(ValueError, match="chengdu_window.*missing required source"):
        build_features(tmp_path / "input", tmp_path / "store")


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


def test_direct_prediction_reconciles_cross_city_event_candidates(tmp_path) -> None:
    observations = [
        NumericObservation(
            START + timedelta(minutes=minute), "node", node, (),
            "node.cpu_usage", 85.0 if 30 <= minute < 38 else 2.0, (), "high",
        )
        for minute in range(70)
        for node in ("chengdu-service-vm-1", "wuhan-service-vm-1")
    ]
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    output = tmp_path / "predictions.jsonl"
    inference = tmp_path / "inference.json"

    run.predict(store, None, output, inference_log=inference)

    predictions = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    report = json.loads(inference.read_text(encoding="utf-8"))
    assert len(predictions) == report["event_count"] == 1
    assert report["event_reconciliation"] == {
        "candidate_count": 2,
        "suppressed_count": 1,
    }
    validate_prediction(predictions[0])


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


def test_direct_timeline_carries_frr_and_netflow_to_root_ranking_without_netflow_trigger(tmp_path) -> None:
    root = "chengdu-br-1"
    observations = []
    for minute in range(80):
        timestamp = START + timedelta(minutes=minute)
        observations.extend((
            NumericObservation(timestamp, "node", root, (), "node.cpu_usage", 45.0 if 30 <= minute < 36 else 2.0, (), "high"),
            NumericObservation(timestamp, "netflow", root, (), "netflow.flow_records", 40.0, (("protocol", "6"), ("interface_id", "eth0")), "high"),
            NumericObservation(timestamp, "netflow", root, (), "netflow.protocol_byte_share", 0.9 if 30 <= minute < 36 else 0.2, (("protocol", "6"), ("interface_id", "eth0")), "high"),
        ))
    observations.append(NumericObservation(
        START + timedelta(minutes=32), "frr", root, (), "frr.event_count", 2.0,
        (("event_family", "bgp"), ("severity", "err")), "state",
    ))
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "support-store"
    )
    direct = score_direct_evidence(store)

    timeline, decoder_evidence = _direct_timeline(store, direct)
    event = DecodedEvent(
        30, 35, 32, 0.95, 3.0,
        START + timedelta(minutes=30), START + timedelta(minutes=36),
    )
    ranking = rank_root_causes(event, timeline, store)

    assert timeline.log[32, store.entities.node_index(root)] > 0
    assert timeline.edge[32].max() > 0
    assert ranking.explanations[root]["log"] > 0
    assert ranking.explanations[root]["netflow"] > 0
    assert direct.global_probability[20] < 0.8
    assert decoder_evidence.edge is not timeline.edge


def test_disk_label_without_top1_direct_anchor_is_flagged_low_confidence(tmp_path) -> None:
    root = "beida-service-vm-1"
    symptom = "wuhan-service-vm-2"
    observations = []
    for minute in range(70):
        timestamp = START + timedelta(minutes=minute)
        observations.extend((
            NumericObservation(timestamp, "node", root, (), "node.open_fd_ratio", 0.1, (), "high"),
            NumericObservation(timestamp, "node", root, (), "node.disk_io_util", 3.0, (), "high"),
            NumericObservation(timestamp, "node", symptom, (), "node.disk_io_util",
                               95.0 if 30 <= minute < 36 else 2.0, (), "high"),
        ))
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    direct = score_direct_evidence(store)
    node_scores = np.zeros((70, 80), dtype=np.float32)
    node_scores[30:36, store.entities.node_index(root)] = 20.0
    timeline = TimelineScores(
        family=np.ones((70, 3), dtype=np.float32), node=node_scores,
        edge=np.zeros((70, len(store.entities.edges)), dtype=np.float32),
        log=np.zeros((70, 80), dtype=np.float32), coverage=np.ones(70, dtype=np.int32),
    )
    event = DecodedEvent(30, 35, 32, 0.95, 3.0,
                         START + timedelta(minutes=30), START + timedelta(minutes=36))

    records, audit = build_prediction_records(
        (event,), timeline, store, load_public_config("fault_taxonomy"),
        direct_evidence=direct,
    )

    assert records[0]["root_cause_top5"][0]["network_element_id"] == root
    assert records[0]["fault_category"] == {
        "major_category": "resource", "sub_category": "disk_io_pressure"
    }
    assert audit[0]["classification"]["category_anchor_status"] == "unsupported"
    assert audit[0]["classification"]["confidence"] <= 0.5
    assert audit[0]["classification"]["top3"][0][1] <= 0.5


def test_detector_only_comparison_reuses_direct_diagnosis_for_neural_events(tmp_path) -> None:
    root = "beida-service-vm-1"
    observations = [
        NumericObservation(
            START + timedelta(minutes=minute), "node", root, (), "node.cpu_usage",
            80.0 if 30 <= minute < 36 else 2.0, (), "high",
        )
        for minute in range(70)
    ]
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    neural_node = np.zeros((70, 80), dtype=np.float32)
    neural_node[:, store.entities.node_index("wuhan-service-vm-2")] = 20.0
    neural_timeline = TimelineScores(
        family=np.zeros((70, 3), dtype=np.float32),
        node=neural_node,
        edge=np.zeros((70, 0), dtype=np.float32),
        log=np.zeros((70, 80), dtype=np.float32),
        coverage=np.ones(70, dtype=np.int32),
    )
    event = DecodedEvent(
        30, 35, 32, 0.9, 2.0,
        START + timedelta(minutes=30), START + timedelta(minutes=36),
    )
    direct = score_direct_evidence(store)
    direct_timeline, _ = _direct_timeline(store, direct)
    expected, _ = build_prediction_records(
        (event,), direct_timeline, store, load_public_config("fault_taxonomy"),
        direct_evidence=direct,
    )

    comparison_timeline, comparison_direct, comparison_heads = run._diagnosis_inputs(
        store, neural_timeline, None, object(), comparison_mode="detector-only"
    )
    actual, audit = build_prediction_records(
        (event,), comparison_timeline, store, load_public_config("fault_taxonomy"),
        diagnosis_heads=comparison_heads, direct_evidence=comparison_direct,
    )

    assert actual == expected
    assert actual[0]["root_cause_top5"][0]["network_element_id"] == root
    assert audit[0]["classification"]["direct_category_override"] == {
        "major_category": "resource", "sub_category": "cpu_pressure"
    }


def test_native_postprocessing_keeps_neural_timeline_and_heads(tmp_path) -> None:
    store = build_feature_store(
        [NumericObservation(START, "node", "beida-br-1", (), "node.cpu_usage", 2.0, (), "high")],
        load_public_config("network_elements"), tmp_path / "store",
    )
    timeline = TimelineScores(
        family=np.zeros((1, 3), dtype=np.float32),
        node=np.zeros((1, 80), dtype=np.float32),
        edge=np.zeros((1, 0), dtype=np.float32),
        log=np.zeros((1, 80), dtype=np.float32),
        coverage=np.ones(1, dtype=np.int32),
    )
    heads = object()
    selected_timeline, direct, selected_heads = run._diagnosis_inputs(
        store, timeline, None, heads, comparison_mode="native"
    )

    assert selected_timeline is timeline
    assert direct is None
    assert selected_heads is heads


def test_predict_cli_accepts_detector_only_comparison() -> None:
    args = run._parser().parse_args([
        "predict", "--store", "store", "--checkpoint", "model.pt",
        "--detector", "neural", "--comparison-mode", "detector-only",
        "--output", "predictions.jsonl",
    ])

    assert args.comparison_mode == "detector-only"


def test_predict_cli_accepts_separate_neural_score_audit() -> None:
    args = run._parser().parse_args([
        "predict", "--store", "store", "--checkpoint", "model.pt",
        "--detector", "neural", "--output", "predictions.jsonl",
        "--score-audit", "neural_score_audit.json",
    ])

    assert args.score_audit == Path("neural_score_audit.json")


def test_detector_only_predict_keeps_checkpoint_head_availability_distinct_from_usage(tmp_path) -> None:
    observations = [
        NumericObservation(
            START + timedelta(minutes=minute), "node", "beida-service-vm-1", (),
            "node.cpu_usage", 70.0 if minute in (2, 3) else 2.0, (), "high",
        )
        for minute in range(6)
    ]
    observations.extend(
        NumericObservation(
            START + timedelta(minutes=minute), "traffic", "beida-traffic-vm",
            ("beida-service-vm-1",), "traffic.web.latency_p95_seconds", 0.2,
            (("target_region", "beida"), ("flow_type", "web")), "high",
        )
        for minute in range(6)
    )
    observations.append(NumericObservation(
        START + timedelta(minutes=2), "frr", "beida-br-1", (), "frr.event_count", 1.0,
        (("event_family", "bgp"), ("severity", "err")), "state",
    ))
    store = build_feature_store(
        observations, load_public_config("network_elements"), tmp_path / "store"
    )
    artifact = train_detector(
        store,
        TrainConfig(epochs=1, batch_size=2, window_minutes=4, stride_minutes=2, hidden_size=8),
        taxonomy=load_public_config("fault_taxonomy"),
        diagnosis_examples_per_category=1,
        diagnosis_epochs=1,
    )
    checkpoint = tmp_path / "checkpoint.pt"
    save_checkpoint(artifact, checkpoint)
    output = tmp_path / "predictions.jsonl"
    inference = tmp_path / "inference.json"
    score_audit = tmp_path / "score_audit.json"

    run.predict(
        store, checkpoint, output, inference_log=inference, score_audit=score_audit,
        detector="neural", comparison_mode="detector-only",
    )

    metadata = json.loads(inference.read_text(encoding="utf-8"))["run_metadata"]
    scores = json.loads(score_audit.read_text(encoding="utf-8"))
    assert metadata["diagnosis_head_available"] is True
    assert metadata["diagnosis_head_used"] is False
    assert metadata["comparison_mode"] == "detector-only"
    assert scores["summary"]["total_minute_count"] == 6

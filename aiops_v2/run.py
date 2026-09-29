"""Command-line entry point for the v2 tensorized diagnosis pipeline."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

from aiops_challenge_2026.config import load_public_config
from aiops_challenge_2026.schema import validate_prediction
from aiops_v2.classification.semantic import classify_event
from aiops_v2.data.feature_store import FeatureStore, build_feature_store
from aiops_v2.data.source import CanonicalObservationStream, iter_source_files
from aiops_v2.data.windows import WindowDataset
from aiops_v2.detection import DirectEvidence, score_direct_evidence
from aiops_v2.detection.direct_evidence import select_specific_category
from aiops_v2.detection.source_support import score_frr_support, score_netflow_support
from aiops_v2.evidence_audit import audit_run
from aiops_v2.events.decoder import (
    DecodedEvent,
    DecoderConfig,
    DecoderEvidence,
    decode_events,
    reconcile_events,
)
from aiops_v2.events.city_decoder import decode_city_events
from aiops_v2.localization.ranking import rank_root_causes
from aiops_v2.models.llm_review import (
    EventReview,
    OpenAICompatibleBackend,
    OpenAICompatibleConfig,
    TransformersBackend,
)
from aiops_v2.score_audit import build_score_audit
from aiops_v2.training.inference import TimelineScores, score_timeline
from aiops_v2.training.entity_calibration import calibrated_family_scores
from aiops_v2.training.trainer import (
    TrainConfig,
    load_checkpoint,
    save_checkpoint,
    train_detector,
)


def _utc(value) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_json(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_predictions(records: list[dict[str, Any]], path: Path) -> None:
    for record in records:
        validate_prediction(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def build_features(data_root: Path, store_path: Path) -> FeatureStore:
    network = load_public_config("network_elements")
    required_sources = {"node", "interface", "routing", "scrape", "traffic", "netflow", "frr"}
    sources_by_bundle: dict[Path, set[str]] = {}
    for source, path in iter_source_files(data_root):
        sources_by_bundle.setdefault(path.parent.parent, set()).add(source)
    if not sources_by_bundle:
        raise ValueError(f"missing required source files under {data_root}")
    for bundle, found_sources in sorted(sources_by_bundle.items()):
        missing_sources = required_sources - found_sources
        if missing_sources:
            raise ValueError(
                f"{bundle}: missing required source files: {sorted(missing_sources)}"
            )
    aliases = {city: city for city in network["cities"]}
    stream = CanonicalObservationStream(
        data_root,
        aliases=aliases,
        valid_roles=network["device_roles"],
    )
    return build_feature_store(stream, network, store_path)


def build_prediction_records(
    events: tuple[DecodedEvent, ...],
    timeline: TimelineScores,
    store: FeatureStore,
    taxonomy: dict[str, Any],
    *,
    prefix: str = "pred_",
    reviewer: EventReview | None = None,
    diagnosis_heads=None,
    device: str = "cpu",
    direct_evidence: DirectEvidence | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records = []
    audit = []
    for index, event in enumerate(events, 1):
        ranking = rank_root_causes(
            event,
            timeline,
            store,
            diagnosis_heads=diagnosis_heads,
            device=device,
        )
        classification = classify_event(
            event,
            ranking.top5[0],
            store,
            taxonomy,
            ranking=ranking,
            diagnosis_heads=diagnosis_heads,
            device=device,
        )
        direct_category = None
        category_anchor_status = "not_checked"
        category_anchor_strength = None
        if direct_evidence is not None:
            root_index = store.entities.node_index(ranking.top5[0])
            start = max(0, event.start_index)
            stop = min(direct_evidence.category_scores.shape[0], event.end_index + 1)
            strengths = direct_evidence.category_scores[start:stop, root_index].max(axis=0)
            family = select_specific_category(strengths, direct_evidence.category_names)
            official_family = (
                "disk_space_low" if family == "disk_space_pressure" else family
            )
            valid = {
                (item["major_category"], item["sub_category"])
                for item in taxonomy["fault_categories"]
            }
            root_node = ranking.top5[0]
            if official_family == "cpu_pressure" and root_node.endswith("-fw") and ("firewall", official_family) in valid:
                direct_category = {"major_category": "firewall", "sub_category": official_family}
            elif official_family is not None and ("resource", official_family) in valid and not (
                root_node.endswith("-fw") or "-br-" in root_node or "-cr-" in root_node
            ):
                direct_category = {
                    "major_category": "resource",
                    "sub_category": official_family,
                }
            if direct_category is not None:
                direct_confidence = max(
                    classification.confidence,
                    float(direct_evidence.node_probability[start:stop, root_index].max()),
                )
                selected_name = next(
                    item["fault_name"]
                    for item in taxonomy["fault_categories"]
                    if item["major_category"] == direct_category["major_category"]
                    and item["sub_category"] == direct_category["sub_category"]
                )
                remaining = tuple(
                    entry for entry in classification.top3 if entry[0] != selected_name
                )
                classification = replace(
                    classification,
                    category=direct_category,
                    confidence=direct_confidence,
                    top3=((selected_name, direct_confidence),) + remaining[:2],
                )
            if classification.category == {
                "major_category": "resource", "sub_category": "disk_io_pressure"
            }:
                disk_feature = "node.disk_io_util"
                names = store.features.names("node")
                observed_disk = (
                    disk_feature in names
                    and bool(store.node_mask[start:stop, root_index, names.index(disk_feature)].any())
                )
                category_anchor_strength = float(strengths[direct_evidence.category_names.index("disk_io_pressure")])
                category_anchor_status = (
                    "supported" if category_anchor_strength >= 5.0 else
                    "unsupported" if observed_disk else "undetermined"
                )
                if category_anchor_status != "supported":
                    # The category may still be the best legal choice, but
                    # cross-candidate evidence must not masquerade as a
                    # high-confidence root-local disk diagnosis.
                    adjusted_confidence = min(classification.confidence, 0.5)
                    classification = replace(
                        classification,
                        confidence=adjusted_confidence,
                        top3=((classification.top3[0][0], adjusted_confidence),) + classification.top3[1:],
                    )
        local_category = dict(classification.category)
        review = None
        top_scores = [ranking.scores[node] for node in ranking.top5]
        root_margin = (
            (top_scores[0] - top_scores[1]) / max(abs(top_scores[0]), 1e-8)
            if len(top_scores) > 1
            else 1.0
        )
        needs_review = (
            event.confidence < 0.75
            or classification.confidence < 0.60
            or root_margin < 0.12
        )
        if reviewer is not None and needs_review:
            evidence = [
                {
                    "id": f"root:{node}",
                    "kind": "root_candidate",
                    "node_id": node,
                    "score": ranking.scores[node],
                    "components": ranking.explanations[node],
                }
                for node in ranking.top5
            ]
            evidence.extend(
                {
                    "id": f"signal:{name}",
                    "kind": "classification_signal",
                    "strength": float(strength),
                }
                for name, strength in sorted(classification.signals.items())
                if strength > 0
            )
            evidence.extend(
                {
                    "id": f"edge:{edge_index:02d}",
                    "kind": "event_subgraph_edge",
                    **edge,
                }
                for edge_index, edge in enumerate(ranking.event_subgraph["edges"])
            )
            payload = {
                "event": {
                    "start_time": _utc(event.start_time),
                    "end_time": _utc(event.end_time),
                    "duration_minutes": event.duration_minutes,
                    "local_confidence": event.confidence,
                    "decoder_score": event.score,
                },
                "root_candidates": [
                    {
                        "network_element_id": node,
                        "score": ranking.scores[node],
                        "components": ranking.explanations[node],
                    }
                    for node in ranking.top5
                ],
                "local_category": local_category,
                "local_category_top3": [
                    {"fault_name": name, "score": float(score)}
                    for name, score in classification.top3
                ],
                "event_subgraph": ranking.event_subgraph,
                "allowed_categories": [
                    {
                        "major_category": item["major_category"],
                        "sub_category": item["sub_category"],
                    }
                    for item in taxonomy["fault_categories"]
                ],
                "evidence": evidence,
                "allowed_evidence_ids": [item["id"] for item in evidence],
            }
            review = reviewer.review(
                payload,
                local_roots=ranking.top5,
                local_category=local_category,
            )
        final_roots = review.root_cause_top5 if review is not None else ranking.top5
        final_category = review.fault_category if review is not None else local_category
        prediction_id = f"{prefix}{index:06d}"
        record = {
            "prediction_id": prediction_id,
            "start_time": _utc(event.start_time),
            "end_time": _utc(event.end_time),
            "root_cause_top5": [
                {"rank": rank, "network_element_id": node}
                for rank, node in enumerate(final_roots, 1)
            ],
            "fault_category": final_category,
        }
        validate_prediction(record)
        records.append(record)
        audit.append(
            {
                "prediction_id": prediction_id,
                "detection_sources": (
                    {
                        "direct_probability_peak": float(
                            direct_evidence.node_probability[
                                event.start_index : event.end_index + 1,
                                [i for i, node in enumerate(store.entities.nodes)
                                 if event.city_id is None or node.startswith(f"{event.city_id}-")],
                            ].max()
                        ),
                        "service_symptom_strength_peak": float(
                            direct_evidence.edge_symptom_scores[
                                event.start_index : event.end_index + 1,
                                [i for i, edge in enumerate(store.entities.edges)
                                 if event.city_id is None or (
                                     edge.relation == "traffic"
                                     and edge.target.startswith(f"service-group:{event.city_id}:")
                                 )],
                            ].max()
                        ) if direct_evidence.edge_symptom_scores is not None
                        and direct_evidence.edge_symptom_scores.size else 0.0,
                    }
                    if direct_evidence is not None else None
                ),
                "event": {
                    "start_index": event.start_index,
                    "end_index": event.end_index,
                    "peak_index": event.peak_index,
                    "city_id": event.city_id,
                    "root_city_scoped": event.root_city_scoped,
                    "confidence": event.confidence,
                    "decoder_score": event.score,
                    "decoder_evidence": event.evidence_scores,
                },
                "root_scores": [
                    {
                        "node_id": node,
                        "score": ranking.scores[node],
                        "components": ranking.explanations[node],
                    }
                    for node in ranking.top5
                ],
                "event_subgraph": ranking.event_subgraph,
                "classification": {
                    "local_category": local_category,
                    "category": final_category,
                    "confidence": classification.confidence,
                    "top3": list(classification.top3),
                    "signals": classification.signals,
                    "candidate_category_scores": classification.candidate_category_scores,
                    "direct_category_override": direct_category,
                    "category_anchor_status": category_anchor_status,
                    "category_anchor_strength": category_anchor_strength,
                },
                "llm_review": (
                    {
                        "status": review.status,
                        "event_opinion": review.event_opinion,
                        "root_cause_top5": list(review.root_cause_top5),
                        "fault_category": review.fault_category,
                        "suggested_root_cause_top5": (
                            list(review.suggested_root_cause_top5)
                            if review.suggested_root_cause_top5 is not None
                            else None
                        ),
                        "suggested_fault_category": review.suggested_fault_category,
                        "evidence_ids": list(review.evidence_ids),
                        "confidence": review.confidence,
                        "rationale": review.rationale,
                        "fallback_reason": review.fallback_reason,
                        "event_mutation_allowed": False,
                    }
                    if review is not None
                    else {
                        "status": "skipped_confident" if reviewer is not None else "disabled",
                        "event_mutation_allowed": False,
                    }
                ),
            }
        )
    return records, audit


def _check_checkpoint_compatibility(store: FeatureStore, feature_manifest: dict[str, Any] | None) -> None:
    if feature_manifest is None:
        return
    for key in ("entities", "features"):
        if feature_manifest.get(key) != store.manifest.get(key):
            raise ValueError(f"checkpoint and feature store have different {key}")


def _direct_timeline(store: FeatureStore, direct_evidence: DirectEvidence) -> tuple[TimelineScores, DecoderEvidence]:
    """Expose log/NetFlow RCA support without making NetFlow an event trigger."""
    import numpy as np

    minute_count, _ = direct_evidence.node_probability.shape
    edge_symptoms = direct_evidence.edge_symptom_scores
    if edge_symptoms is None:
        edge_symptoms = np.zeros((minute_count, len(store.entities.edges)), dtype=np.float32)
    netflow_support = score_netflow_support(store)
    log_support = score_frr_support(store)
    family = np.zeros((minute_count, 3), dtype=np.float32)
    family[:, 0] = np.max(direct_evidence.category_scores, axis=(1, 2))
    if edge_symptoms.shape[1]:
        family[:, 1] = np.max(edge_symptoms, axis=1)
    observed = np.zeros((minute_count, 3), dtype=bool)
    observed[:, 0] = np.any(direct_evidence.observed, axis=1)
    if edge_symptoms.shape[1]:
        observed[:, 1] = np.any(edge_symptoms > 0, axis=1)
    timeline = TimelineScores(
        family=family,
        node=np.max(direct_evidence.category_scores, axis=2),
        edge=np.maximum(edge_symptoms, netflow_support),
        log=log_support,
        coverage=np.ones(minute_count, dtype=np.int32),
        family_observed=observed,
        components={"netflow_support": netflow_support, "service_symptoms": edge_symptoms},
    )
    decoder_evidence = DecoderEvidence(
        family_z_scores=family,
        family_observed=observed,
        node=timeline.node,
        edge=edge_symptoms,
        log=log_support,
    )
    return timeline, decoder_evidence


def _diagnosis_inputs(
    store: FeatureStore,
    timeline: TimelineScores,
    direct_evidence: DirectEvidence | None,
    diagnosis_heads,
    *,
    comparison_mode: str,
) -> tuple[TimelineScores, DirectEvidence | None, Any]:
    """Hold RCA/classification fixed when comparing detector event boundaries."""
    if comparison_mode == "native":
        return timeline, direct_evidence, diagnosis_heads
    if comparison_mode != "detector-only":
        raise ValueError(f"unknown comparison mode: {comparison_mode}")
    if direct_evidence is not None:
        return timeline, direct_evidence, None
    direct_evidence = score_direct_evidence(store)
    direct_timeline, _ = _direct_timeline(store, direct_evidence)
    return direct_timeline, direct_evidence, None


def predict(
    store: FeatureStore,
    checkpoint: Path | None,
    output: Path,
    *,
    inference_log: Path | None = None,
    device: str = "cpu",
    decoder_config: DecoderConfig | None = None,
    reviewer: EventReview | None = None,
    detector: str = "direct",
    comparison_mode: str = "native",
    score_audit: Path | None = None,
) -> int:
    if score_audit is not None and detector != "neural":
        raise ValueError("--score-audit currently requires --detector neural")
    taxonomy = load_public_config("fault_taxonomy")
    expected_taxonomy = tuple(
        item["fault_name"] for item in taxonomy["fault_categories"]
    )
    artifact = None
    direct_evidence = None
    edge_trigger_eligibility = None
    if detector == "direct":
        direct_evidence = score_direct_evidence(store)
        timeline, decoder_evidence = _direct_timeline(store, direct_evidence)
        probabilities = direct_evidence.global_probability
    elif detector == "neural":
        if checkpoint is None:
            raise ValueError("neural detector requires --checkpoint")
        artifact = load_checkpoint(checkpoint, device=device)
        _check_checkpoint_compatibility(store, artifact.feature_manifest)
        if artifact.diagnosis_heads is not None and artifact.taxonomy_identity != expected_taxonomy:
            raise ValueError("checkpoint taxonomy identity does not match official taxonomy")
        dataset = WindowDataset(
            store,
            window_minutes=artifact.config.window_minutes,
            stride_minutes=artifact.config.stride_minutes,
            scalers=artifact.scalers,
        )
        timeline = score_timeline(artifact.model, dataset, device=device)
        if artifact.entity_calibrators:
            direct_evidence = score_direct_evidence(store)
            node_observed = np.asarray(store.node_mask).any(axis=2)
            edge_observed = np.asarray(store.edge_mask).any(axis=2)
            log_observed = np.asarray(store.log_mask).any(axis=2)
            traffic_edges = np.asarray(
                [edge.relation == "traffic" for edge in store.entities.edges],
                dtype=bool,
            )
            service_gate = direct_evidence.edge_symptom_scores
            if service_gate is None:
                service_gate = np.zeros_like(timeline.edge, dtype=bool)
            else:
                service_gate = service_gate > 0
            edge_trigger_eligibility = (
                edge_observed
                & traffic_edges[None, :]
                & service_gate
            )
            family_scores, family_observed = calibrated_family_scores(
                node_scores=timeline.node,
                edge_scores=timeline.edge,
                log_scores=timeline.log,
                node_observed=node_observed,
                edge_observed=edge_observed,
                log_observed=log_observed,
                calibrators=artifact.entity_calibrators,
                traffic_edges=traffic_edges,
                service_gate=service_gate,
                node_gate=direct_evidence.node_probability >= 0.5,
                log_gate=direct_evidence.node_probability >= 0.5,
            )
            timeline = replace(
                timeline, family=family_scores, family_observed=family_observed
            )
        probabilities = artifact.calibrator.transform(timeline.family, timeline.family_observed)
        decoder_evidence = DecoderEvidence(
            family_z_scores=artifact.calibrator.standardize(timeline.family, timeline.family_observed),
            family_observed=timeline.family_observed,
            node=timeline.node,
            edge=timeline.edge,
            log=timeline.log,
        )
    else:
        raise ValueError(f"unknown detector: {detector}")
    if detector == "direct":
        candidate_events = decode_city_events(
            direct_evidence,
            store.entities.nodes,
            store.entities.edges,
            store.start_time,
            decoder_config,
        )
        events = reconcile_events(candidate_events)
    else:
        candidate_events = decode_events(
            probabilities,
            store.start_time,
            decoder_config,
            evidence=decoder_evidence,
        )
        events = candidate_events
    diagnosis_timeline, diagnosis_direct, diagnosis_heads = _diagnosis_inputs(
        store,
        timeline,
        direct_evidence,
        artifact.diagnosis_heads if artifact is not None else None,
        comparison_mode=comparison_mode,
    )
    records, audit = build_prediction_records(
        events,
        diagnosis_timeline,
        store,
        taxonomy,
        reviewer=reviewer,
        diagnosis_heads=diagnosis_heads,
        device=device,
        direct_evidence=diagnosis_direct,
    )
    write_predictions(records, output)
    score_audit_summary = None
    if score_audit is not None:
        assert artifact is not None
        score_report = build_score_audit(
            probabilities,
            timeline,
            artifact.calibrator.standardize(timeline.family, timeline.family_observed),
            store.start_time,
            nodes=store.entities.nodes,
            edges=store.entities.edges,
            edge_trigger_eligibility=edge_trigger_eligibility,
        )
        _write_json(score_report, score_audit)
        score_audit_summary = score_report["summary"]
    if inference_log is not None:
        _write_json(
            {
                "format_version": 3,
                "minute_count": int(probabilities.shape[0]),
                "probability_summary": {
                    "minimum": float(probabilities.min()),
                    "median": float(__import__("numpy").median(probabilities)),
                    "maximum": float(probabilities.max()),
                },
                "calibration_summary": (
                    artifact.calibrator.summary.to_dict()
                    if artifact is not None and artifact.calibrator.summary is not None
                    else None
                ),
                "direct_feature_audit": direct_evidence.feature_audit if direct_evidence else None,
                "event_count": len(events),
                "event_reconciliation": {
                    "candidate_count": len(candidate_events),
                    "suppressed_count": len(candidate_events) - len(events),
                },
                "llm_review_status_counts": dict(
                    sorted(Counter(item["llm_review"]["status"] for item in audit).items())
                ),
                "run_metadata": {
                    "pipeline_version": "2.0.0",
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "device": device,
                    "detector": detector,
                    "comparison_mode": comparison_mode,
                    "checkpoint_sha256": _sha256_file(checkpoint) if checkpoint is not None and artifact is not None else None,
                    "feature_manifest_sha256": _sha256_file(store.path / "manifest.json"),
                    "feature_store_shapes": store.manifest.get("shapes", {}),
                    "source_counts": store.manifest.get("source_counts", {}),
                    "parser_audit": store.manifest.get("parser_audit"),
                    "train_config": {
                        "window_minutes": artifact.config.window_minutes,
                        "stride_minutes": artifact.config.stride_minutes,
                        "hidden_size": artifact.config.hidden_size,
                        "temporal_layers": artifact.config.temporal_layers,
                        "seed": artifact.config.seed,
                    } if artifact is not None else None,
                    "diagnosis_training": (
                        artifact.diagnosis_summary.to_dict()
                        if artifact is not None and artifact.diagnosis_summary is not None
                        else None
                    ),
                    "diagnosis_head_available": artifact is not None and artifact.diagnosis_heads is not None,
                    "diagnosis_head_used": diagnosis_heads is not None,
                    "score_audit": score_audit_summary,
                    "llm_review": type(reviewer.backend).__name__ if reviewer else "disabled",
                },
                "events": audit,
            },
            inference_log,
        )
    return len(records)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-features")
    build.add_argument("--data-root", type=Path, required=True)
    build.add_argument("--store", type=Path, required=True)

    train = commands.add_parser("train")
    train.add_argument("--store", type=Path, required=True)
    train.add_argument("--checkpoint", type=Path, required=True)
    train.add_argument("--epochs", type=int, default=5)
    train.add_argument("--batch-size", type=int, default=8)
    train.add_argument("--window-minutes", type=int, default=120)
    train.add_argument("--stride-minutes", type=int, default=30)
    train.add_argument("--hidden-size", type=int, default=64)
    train.add_argument("--device", default="cpu")
    train.add_argument("--scaler-transform", choices=("linear", "asinh"), default="linear")
    train.add_argument("--score-pooling", choices=("mean", "topk"), default="mean")
    train.add_argument("--validation-fraction", type=float, default=0.0)

    prediction = commands.add_parser("predict")
    prediction.add_argument("--store", type=Path, required=True)
    prediction.add_argument("--checkpoint", type=Path)
    prediction.add_argument("--detector", choices=("direct", "neural"), default="direct")
    prediction.add_argument("--comparison-mode", choices=("native", "detector-only"), default="native")
    prediction.add_argument("--output", type=Path, required=True)
    prediction.add_argument("--inference-log", type=Path)
    prediction.add_argument("--score-audit", type=Path, help="neural high-score minute attribution JSON")
    prediction.add_argument("--device", default="cpu")
    _add_llm_options(prediction)

    audit = commands.add_parser("audit")
    audit.add_argument("--store", type=Path, required=True)
    audit.add_argument("--predictions", type=Path, required=True)
    audit.add_argument("--inference-log", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)

    complete = commands.add_parser("all")
    complete.add_argument("--data-root", type=Path, required=True)
    complete.add_argument("--store", type=Path, required=True)
    complete.add_argument("--checkpoint", type=Path)
    complete.add_argument("--detector", choices=("direct", "neural"), default="direct")
    complete.add_argument("--comparison-mode", choices=("native", "detector-only"), default="native")
    complete.add_argument("--output", type=Path, required=True)
    complete.add_argument("--inference-log", type=Path)
    complete.add_argument("--score-audit", type=Path, help="neural high-score minute attribution JSON")
    complete.add_argument("--epochs", type=int, default=5)
    complete.add_argument("--batch-size", type=int, default=8)
    complete.add_argument("--window-minutes", type=int, default=120)
    complete.add_argument("--stride-minutes", type=int, default=30)
    complete.add_argument("--hidden-size", type=int, default=64)
    complete.add_argument("--device", default="cpu")
    complete.add_argument("--scaler-transform", choices=("linear", "asinh"), default="linear")
    complete.add_argument("--score-pooling", choices=("mean", "topk"), default="mean")
    complete.add_argument("--validation-fraction", type=float, default=0.0)
    _add_llm_options(complete)
    return parser


def _add_llm_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--llm-backend",
        choices=("none", "openai-compatible", "transformers"),
        default="none",
        help="optional constrained review of local event RCA/category (never event boundaries)",
    )
    parser.add_argument("--llm-model", default="")
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("AIOPS_LLM_BASE_URL", ""),
    )
    parser.add_argument(
        "--llm-api-key-env",
        default="AIOPS_LLM_API_KEY",
        help="environment variable containing the API key; the key itself is never a CLI arg",
    )
    parser.add_argument("--llm-timeout", type=float, default=45.0)
    parser.add_argument(
        "--llm-thinking-mode",
        choices=("provider-default", "enabled", "disabled"),
        default="provider-default",
        help="thinking mode for OpenAI-compatible APIs that support it, such as DeepSeek",
    )


def _reviewer_from_args(args, taxonomy: dict[str, Any]) -> EventReview | None:
    backend_name = getattr(args, "llm_backend", "none")
    if backend_name == "none":
        return None
    if backend_name == "openai-compatible":
        backend = OpenAICompatibleBackend(
            OpenAICompatibleConfig(
                base_url=args.llm_base_url,
                model=args.llm_model,
                api_key_env=args.llm_api_key_env,
                timeout_seconds=args.llm_timeout,
                thinking_mode=args.llm_thinking_mode,
            )
        )
    else:
        backend = TransformersBackend(args.llm_model)
    # The v2 evidence graph makes API review prompts larger than the local
    # model's conservative cap; keep the API bound finite but large enough for
    # the current event payloads.
    max_prompt_chars = 50_000 if backend_name == "openai-compatible" else 20_000
    return EventReview(backend, taxonomy, max_prompt_chars=max_prompt_chars)


def _train_from_args(args, store: FeatureStore, taxonomy: dict[str, Any]):
    config = TrainConfig(
        epochs=args.epochs,
        batch_size=args.batch_size,
        window_minutes=args.window_minutes,
        stride_minutes=args.stride_minutes,
        hidden_size=args.hidden_size,
        device=args.device,
        scaler_transform=args.scaler_transform,
        score_pooling=args.score_pooling,
        validation_fraction=args.validation_fraction,
    )
    artifact = train_detector(store, config, taxonomy=taxonomy)
    save_checkpoint(artifact, args.checkpoint, feature_manifest=store.manifest)
    return artifact


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "build-features":
            store = build_features(args.data_root, args.store)
            result = {"store": str(args.store), "manifest": store.manifest}
        elif args.command == "audit":
            store = FeatureStore.open(args.store)
            report = audit_run(store, args.predictions, args.inference_log)
            _write_json(report, args.output)
            result = {"audit": str(args.output), **report["summary"]}
        elif args.command == "train":
            store = FeatureStore.open(args.store)
            taxonomy = load_public_config("fault_taxonomy")
            artifact = _train_from_args(args, store, taxonomy)
            result = {
                "checkpoint": str(args.checkpoint),
                "loss_history": list(artifact.history),
                "validation_loss_history": list(artifact.validation_history),
                "temporal_split": (
                    asdict(artifact.temporal_split) if artifact.temporal_split else None
                ),
                "diagnosis_training": (
                    artifact.diagnosis_summary.to_dict()
                    if artifact.diagnosis_summary is not None
                    else None
                ),
            }
        elif args.command == "predict":
            store = FeatureStore.open(args.store)
            taxonomy = load_public_config("fault_taxonomy")
            count = predict(
                store,
                args.checkpoint,
                args.output,
                inference_log=args.inference_log,
                device=args.device,
                reviewer=_reviewer_from_args(args, taxonomy),
                detector=args.detector,
                comparison_mode=args.comparison_mode,
                score_audit=args.score_audit,
            )
            result = {"events": count, "output": str(args.output)}
        else:
            store = build_features(args.data_root, args.store)
            taxonomy = load_public_config("fault_taxonomy")
            if args.detector == "neural":
                if args.checkpoint is None:
                    raise ValueError("neural detector requires --checkpoint")
                _train_from_args(args, store, taxonomy)
            count = predict(
                store,
                args.checkpoint,
                args.output,
                inference_log=args.inference_log,
                device=args.device,
                reviewer=_reviewer_from_args(args, taxonomy),
                detector=args.detector,
                comparison_mode=args.comparison_mode,
                score_audit=args.score_audit,
            )
            result = {"events": count, "output": str(args.output)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"v2 pipeline failed: {type(exc).__name__}: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

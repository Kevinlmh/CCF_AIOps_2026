"""Command-line entry point for the v2 tensorized diagnosis pipeline."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from aiops_challenge_2026.config import load_public_config
from aiops_challenge_2026.schema import validate_prediction
from aiops_v2.classification.semantic import classify_event
from aiops_v2.data.feature_store import FeatureStore, build_feature_store
from aiops_v2.data.source import CanonicalObservationStream
from aiops_v2.data.windows import WindowDataset
from aiops_v2.events.decoder import DecodedEvent, DecoderConfig, decode_events
from aiops_v2.localization.ranking import rank_root_causes
from aiops_v2.models.llm_review import (
    EventReview,
    OpenAICompatibleBackend,
    OpenAICompatibleConfig,
    TransformersBackend,
)
from aiops_v2.training.inference import TimelineScores, score_timeline
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records = []
    audit = []
    for index, event in enumerate(events, 1):
        ranking = rank_root_causes(event, timeline, store)
        classification = classify_event(event, ranking.top5[0], store, taxonomy)
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
                "event": {
                    "start_index": event.start_index,
                    "end_index": event.end_index,
                    "peak_index": event.peak_index,
                    "confidence": event.confidence,
                    "decoder_score": event.score,
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


def predict(
    store: FeatureStore,
    checkpoint: Path,
    output: Path,
    *,
    inference_log: Path | None = None,
    device: str = "cpu",
    decoder_config: DecoderConfig | None = None,
    reviewer: EventReview | None = None,
) -> int:
    artifact = load_checkpoint(checkpoint, device=device)
    _check_checkpoint_compatibility(store, artifact.feature_manifest)
    dataset = WindowDataset(
        store,
        window_minutes=artifact.config.window_minutes,
        stride_minutes=artifact.config.stride_minutes,
        scalers=artifact.scalers,
    )
    timeline = score_timeline(artifact.model, dataset, device=device)
    probabilities = artifact.calibrator.transform(timeline.family)
    events = decode_events(probabilities, store.start_time, decoder_config)
    records, audit = build_prediction_records(
        events,
        timeline,
        store,
        load_public_config("fault_taxonomy"),
        reviewer=reviewer,
    )
    write_predictions(records, output)
    if inference_log is not None:
        _write_json(
            {
                "format_version": 2,
                "minute_count": int(probabilities.shape[0]),
                "probability_summary": {
                    "minimum": float(probabilities.min()),
                    "median": float(__import__("numpy").median(probabilities)),
                    "maximum": float(probabilities.max()),
                },
                "event_count": len(events),
                "llm_review_status_counts": dict(
                    sorted(Counter(item["llm_review"]["status"] for item in audit).items())
                ),
                "run_metadata": {
                    "pipeline_version": "2.0.0",
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "device": device,
                    "checkpoint_sha256": _sha256_file(checkpoint),
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
                    },
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

    prediction = commands.add_parser("predict")
    prediction.add_argument("--store", type=Path, required=True)
    prediction.add_argument("--checkpoint", type=Path, required=True)
    prediction.add_argument("--output", type=Path, required=True)
    prediction.add_argument("--inference-log", type=Path)
    prediction.add_argument("--device", default="cpu")
    _add_llm_options(prediction)

    complete = commands.add_parser("all")
    complete.add_argument("--data-root", type=Path, required=True)
    complete.add_argument("--store", type=Path, required=True)
    complete.add_argument("--checkpoint", type=Path, required=True)
    complete.add_argument("--output", type=Path, required=True)
    complete.add_argument("--inference-log", type=Path)
    complete.add_argument("--epochs", type=int, default=5)
    complete.add_argument("--batch-size", type=int, default=8)
    complete.add_argument("--window-minutes", type=int, default=120)
    complete.add_argument("--stride-minutes", type=int, default=30)
    complete.add_argument("--hidden-size", type=int, default=64)
    complete.add_argument("--device", default="cpu")
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
            )
        )
    else:
        backend = TransformersBackend(args.llm_model)
    return EventReview(backend, taxonomy)


def _train_from_args(args, store: FeatureStore):
    config = TrainConfig(
        epochs=args.epochs,
        batch_size=args.batch_size,
        window_minutes=args.window_minutes,
        stride_minutes=args.stride_minutes,
        hidden_size=args.hidden_size,
        device=args.device,
    )
    artifact = train_detector(store, config)
    save_checkpoint(artifact, args.checkpoint, feature_manifest=store.manifest)
    return artifact


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "build-features":
            store = build_features(args.data_root, args.store)
            result = {"store": str(args.store), "manifest": store.manifest}
        elif args.command == "train":
            store = FeatureStore.open(args.store)
            artifact = _train_from_args(args, store)
            result = {"checkpoint": str(args.checkpoint), "loss_history": list(artifact.history)}
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
            )
            result = {"events": count, "output": str(args.output)}
        else:
            store = build_features(args.data_root, args.store)
            _train_from_args(args, store)
            taxonomy = load_public_config("fault_taxonomy")
            count = predict(
                store,
                args.checkpoint,
                args.output,
                inference_log=args.inference_log,
                device=args.device,
                reviewer=_reviewer_from_args(args, taxonomy),
            )
            result = {"events": count, "output": str(args.output)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"v2 pipeline failed: {type(exc).__name__}: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

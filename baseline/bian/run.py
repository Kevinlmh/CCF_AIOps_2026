"""Run the multi-source hybrid AIOps diagnosis pipeline."""

from __future__ import annotations

import argparse
from datetime import timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from aiops_challenge_2026.config import load_public_config
from aiops_challenge_2026.schema import validate_prediction
from baseline.bian.anomaly_detector.five_sigma import detect as detect_five_sigma
from baseline.bian.anomaly_detector.robust_detector import DetectionDiagnostics, detect_events
from baseline.bian.classification.classifier import (
    classification_taxonomy,
    classify_with_llm,
)
from baseline.bian.classification.prototype_model import ClassificationResult, classify_event
from baseline.bian.localization.graph_fusion import RankingResult, rank_candidates
from baseline.bian.localization.ranking import (
    rank_stage1,
    stage2_consensus,
    validate_stage1,
    validate_stage2,
)
from baseline.bian.models.api_backend import ApiBackend, ApiConfig
from baseline.bian.models.backend import JsonModelBackend, ModelConfig
from baseline.bian.preprocessing.evidence import public_topology
from baseline.bian.preprocessing.multisource import SOURCE_ORDER, ObservationBundle, load_observations
from baseline.bian.preprocessing.observations import AnomalyEvidence, DetectedEvent


def _config() -> dict[str, Any]:
    config = json.loads((HERE / "config" / "topology.json").read_text(encoding="utf-8"))
    network_elements = load_public_config("network_elements")
    taxonomy = load_public_config("fault_taxonomy")
    config.update(taxonomy)
    config["cities"] = network_elements["cities"]
    config["candidate_roles"] = network_elements["device_roles"]
    config["region_aliases"] = {city: city for city in network_elements["cities"]}
    return config


def _model_config(path: Path | None = None) -> dict[str, Any]:
    target = path or (HERE / "config" / "model_v1.json")
    value = json.loads(target.read_text(encoding="utf-8"))
    for section in ("detector", "localization", "classification"):
        if not isinstance(value.get(section), dict):
            raise ValueError(f"model config is missing object section: {section}")
    return value


def _utc(value) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _device_analysis_validator(value: Any, nodes: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"device_analyses"}:
        raise ValueError("device analysis must contain only device_analyses")
    items = value["device_analyses"]
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list) or len(items) != len(nodes):
        raise ValueError("device analysis must cover every public candidate")
    by_node = {}
    for item in items:
        required = {"node_id", "anomaly_score", "evidence_summary"}
        if not isinstance(item, dict) or not required <= set(item):
            raise ValueError("invalid device analysis item")
        node = item["node_id"]
        score = item["anomaly_score"]
        if node not in nodes or node in by_node or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise ValueError("invalid device analysis node or score")
        if not isinstance(item["evidence_summary"], str):
            raise ValueError("invalid device analysis evidence summary")
        by_node[node] = {"node_id": node, "anomaly_score": float(score), "evidence_summary": item["evidence_summary"]}
    if set(by_node) != set(nodes):
        raise ValueError("device analysis node set differs from candidates")
    return {"device_analyses": [by_node[node] for node in nodes]}


def _quick_validation_stage1(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Create a shortlist for the no-model workflow check."""
    return rank_stage1(context["candidates"], {}, {"min_candidates": 5, "max_candidates": 12, "top_p": 0.85, "weights": {"model_anomaly_score": 0.0, "deterministic_feature_score": 1.0}})[1]


def _topology_for_nodes(topology: dict[str, Any], nodes: list[str]) -> dict[str, Any]:
    """Keep each batched model request bounded while retaining public edges."""
    allowed = set(nodes)
    return {
        "directed": topology.get("directed", False),
        "nodes": [item for item in topology.get("nodes", []) if item.get("node_id") in allowed],
        "edges": [item for item in topology.get("edges", []) if item.get("source") in allowed and item.get("target") in allowed],
    }


def _llm_event(
    event: DetectedEvent,
    context: dict[str, Any],
    config: dict[str, Any],
    backend: Any,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    nodes = [item["node_id"] for item in context["candidates"]]
    compact_candidates = context["candidates"]
    batch_size = config["model"].get("batch_size", 8)
    analyses_items = []
    for batch_index in range(0, len(compact_candidates), batch_size):
        batch = compact_candidates[batch_index : batch_index + batch_size]
        batch_nodes = [item["node_id"] for item in batch]
        analyses = backend.generate_json(
            role="7B-A",
            prompt_name="7b_a_device_analysis",
            payload={"batch_index": batch_index // batch_size + 1, "candidates": batch},
            validator=lambda value, expected=batch_nodes: _device_analysis_validator(value, expected),
            max_new_tokens=config["model"]["device_max_new_tokens"],
        )
        analyses_items.extend(analyses["device_analyses"])
    analysis_by_node = {item["node_id"]: item for item in analyses_items}
    model_scores = {}
    for batch_index in range(0, len(nodes), batch_size):
        batch_nodes = nodes[batch_index : batch_index + batch_size]
        stage1_raw = backend.generate_json(
            role="7B-B",
            prompt_name="7b_b_stage1",
            payload={
                "batch_index": batch_index // batch_size + 1,
                "candidate_evidence": [item for item in compact_candidates if item["node_id"] in batch_nodes],
                "device_analyses": [analysis_by_node[node] for node in batch_nodes],
                "topology": _topology_for_nodes(context["topology"], batch_nodes),
            },
            validator=lambda value, expected=batch_nodes: validate_stage1(value, expected),
            max_new_tokens=config["model"]["stage1_max_new_tokens"],
        )
        model_scores.update(stage1_raw["scores"])
    shortlist = rank_stage1(
        context["candidates"],
        analysis_by_node,
        config["stage1"],
        model_scores=model_scores,
    )[1]
    shortlist_nodes = [item["node_id"] for item in shortlist]
    shortlist_timeline = [item for item in context["timeline"] if item.get("node_id") in set(shortlist_nodes)]
    shortlist_topology = _topology_for_nodes(context["topology"], shortlist_nodes)

    rounds = []
    for round_index in range(1, config["stage2"]["rounds"] + 1):
        stage2_raw = backend.generate_json(
            role="7B-B",
            prompt_name="7b_b_stage2",
            payload={
                "round": round_index,
                "candidates": shortlist,
                "topology": shortlist_topology,
                "timeline": shortlist_timeline,
            },
            validator=lambda value, allowed=shortlist_nodes: validate_stage2(value, allowed),
            max_new_tokens=config["model"]["stage2_max_new_tokens"],
        )
        rounds.append(stage2_raw["candidates"])
    top5, _ = stage2_consensus(rounds, shortlist, config["stage2"]["weights"])

    category, _ = classify_with_llm(
        backend,
        top5=top5,
        context=context,
        taxonomy=classification_taxonomy(config),
        rounds=config["classification"]["rounds"],
        max_new_tokens=config["model"]["classification_max_new_tokens"],
    )
    return top5, category


def _legacy_events(data_root: Path, aliases: dict[str, str]) -> list[DetectedEvent]:
    converted = []
    for raw in detect_five_sigma(data_root, aliases):
        points = tuple(
            AnomalyEvidence(
                timestamp=item["time"],
                source=item["metric"].split(".", 1)[0],
                node_id=item["node"],
                related_node_ids=(),
                metric=item["metric"],
                value=float(item["magnitude"]),
                baseline=0.0,
                score=min(25.0, float(item["magnitude"])),
                direction="both",
                dimensions=(),
            )
            for item in raw["points"]
        )
        if not points:
            continue
        converted.append(
            DetectedEvent(
                start=raw["start"],
                end=raw["end"],
                peak_time=max(points, key=lambda item: item.score).timestamp,
                confidence=min(1.0, max(item.score for item in points) / 10.0),
                evidence=points,
                source_counts={
                    source: sum(item.source == source for item in points)
                    for source in {item.source for item in points}
                },
            )
        )
    return converted


def _hybrid_context(
    event: DetectedEvent,
    ranking: RankingResult,
    topology: dict[str, Any],
    config: dict[str, Any],
    prototype: ClassificationResult,
) -> dict[str, Any]:
    candidates = []
    for candidate in ranking.candidates:
        evidence = [
            {"evidence_id": f"E-{candidate['node_id']}-{index:02d}", **item}
            for index, item in enumerate(candidate["evidence"], 1)
        ]
        candidates.append(
            {
                "node_id": candidate["node_id"],
                "device_role": candidate["device_role"],
                "deterministic_score": min(1.0, float(candidate["score"])),
                "max_magnitude": max((item["score"] for item in evidence), default=0.0),
                "anomaly_count": candidate["anomaly_count"],
                "features": {
                    name: candidate[name]
                    for name in (
                        "severity",
                        "persistence",
                        "precedence",
                        "source_diversity",
                        "directness",
                        "relational_support",
                        "topology_explanation",
                        "symptom_penalty",
                    )
                },
                "evidence": evidence,
            }
        )
    limit = int(config["preprocessing"]["max_timeline_events"])
    timeline = [
        {
            "timestamp_utc": _utc(point.timestamp),
            "node_id": point.node_id,
            "source": point.source,
            "metric": point.metric,
            "score": round(point.score, 5),
        }
        for point in sorted(event.evidence, key=lambda item: (item.timestamp, -item.score))[:limit]
    ]
    return {
        "candidates": candidates,
        "topology": topology,
        "timeline": timeline,
        "window": {"start_time": _utc(event.start), "end_time": _utc(event.end)},
        "prototype_hint": {
            "category": prototype.category,
            "confidence": prototype.confidence,
            "top3": list(prototype.top3),
            "signals": prototype.signals,
        },
    }


def _write_predictions_atomic(records: list[dict[str, Any]], output: Path) -> None:
    """Validate all records before atomically replacing the JSONL destination."""
    for record in records:
        validate_prediction(record)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_json_atomic(value: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _diagnostic_log(
    *,
    bundle: ObservationBundle | None,
    diagnostics: DetectionDiagnostics | None,
    detector_name: str,
    backend_name: str,
    event_logs: list[dict[str, Any]],
) -> dict[str, Any]:
    nonzero_energy = []
    if diagnostics is not None:
        nonzero_energy = [
            {"timestamp_utc": _utc(minute), "energy": round(energy, 5)}
            for minute, energy in diagnostics.minute_energy.items()
            if energy > 0
        ][:4096]
    coverage = (
        {source: bundle.source_coverage.get(source, 0) for source in SOURCE_ORDER}
        if bundle is not None
        else {source: 0 for source in SOURCE_ORDER}
    )
    return {
        "detector": detector_name,
        "backend": backend_name,
        "source_coverage": coverage,
        "bad_rows": dict(bundle.stats.bad_rows_by_source) if bundle is not None else {},
        "warnings": list(bundle.stats.warnings[:100]) if bundle is not None else [],
        "evidence_count": diagnostics.evidence_count if diagnostics is not None else None,
        "minute_energy_nonzero": nonzero_energy,
        "events": event_logs,
    }


def run(
    data_root: Path,
    output: Path,
    model: str = "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
    use_llm: bool = False,
    prediction_prefix: str = "pred_",
    max_events: int | None = None,
    *,
    detector: str = "robust",
    decision_backend: str = "local",
    api_base: str | None = None,
    api_key_env: str = "AIOPS_LLM_API_KEY",
    config_path: Path | None = None,
    inference_log: Path | None = None,
) -> int:
    config = _config()
    model_config = _model_config(config_path)
    network = load_public_config("network_elements")
    taxonomy = load_public_config("fault_taxonomy")
    topology = public_topology(config)

    if detector not in {"robust", "five-sigma"}:
        raise ValueError(f"unsupported detector: {detector}")
    bundle: ObservationBundle | None = None
    diagnostics: DetectionDiagnostics | None = None
    if detector == "robust":
        bundle = load_observations(
            data_root,
            config["region_aliases"],
            network["device_roles"],
        )
        events, diagnostics = detect_events(bundle, model_config["detector"])
    else:
        events = _legacy_events(data_root, config["region_aliases"])
    if max_events is not None:
        events = events[:max_events]

    backend_name = "transformers" if use_llm and decision_backend == "local" else decision_backend
    if backend_name not in {"local", "transformers", "api"}:
        raise ValueError(f"unsupported decision backend: {backend_name}")
    backend: Any = None
    if backend_name == "transformers":
        backend = JsonModelBackend(model, ModelConfig(**config["model"]), HERE / "prompts")
    elif backend_name == "api":
        if not api_base:
            raise ValueError("--api-base is required when --decision-backend api")
        backend = ApiBackend(
            ApiConfig(
                base_url=api_base,
                model=model,
                api_key_env=api_key_env,
                retries=config["model"]["retries"],
            ),
            HERE / "prompts",
        )

    records: list[dict[str, Any]] = []
    event_logs: list[dict[str, Any]] = []
    for index, event in enumerate(events, 1):
        ranking = rank_candidates(event, network, topology, model_config["localization"])
        prototype = classify_event(
            event,
            ranking,
            taxonomy,
            model_config["classification"],
        )
        top5 = ranking.top5
        category = prototype.category
        if backend is not None:
            context = _hybrid_context(event, ranking, topology, config, prototype)
            try:
                top5, category = _llm_event(event, context, config, backend)
            except Exception as exc:
                reason = " ".join(str(exc).splitlines())[:500]
                raise RuntimeError(
                    f"BiAn LLM inference failed for event {index}: "
                    f"{type(exc).__name__}: {reason}"
                ) from None
        records.append(
            {
                "prediction_id": f"{prediction_prefix}{index:06d}",
                "start_time": _utc(event.start),
                "end_time": _utc(event.end),
                "root_cause_top5": top5,
                "fault_category": category,
            }
        )
        event_logs.append(
            {
                "prediction_id": f"{prediction_prefix}{index:06d}",
                "start_time": _utc(event.start),
                "end_time": _utc(event.end),
                "peak_time": _utc(event.peak_time),
                "confidence": event.confidence,
                "source_counts": event.source_counts,
                "candidates": list(ranking.candidates[:10]),
                "prototype_top3": list(prototype.top3),
                "prototype_signals": prototype.signals,
            }
        )
    _write_predictions_atomic(records, output)
    if inference_log is not None:
        _write_json_atomic(
            _diagnostic_log(
                bundle=bundle,
                diagnostics=diagnostics,
                detector_name=detector,
                backend_name=backend_name,
                event_logs=event_logs,
            ),
            inference_log,
        )
    print(
        json.dumps(
            {"events": len(records), "output": str(output), "mode": backend_name},
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("outputs/predictions.jsonl"))
    parser.add_argument("--model", default="deepseek-ai/DeepSeek-R1-Distill-Qwen-7B")
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--prediction-prefix", default="pred_")
    parser.add_argument("--max-events", type=int)
    parser.add_argument("--detector", choices=("robust", "five-sigma"), default="robust")
    parser.add_argument(
        "--decision-backend", choices=("local", "transformers", "api"), default="local"
    )
    parser.add_argument("--api-base")
    parser.add_argument("--api-key-env", default="AIOPS_LLM_API_KEY")
    parser.add_argument("--config-path", type=Path)
    parser.add_argument("--inference-log", type=Path)
    args = parser.parse_args()
    try:
        return run(
            args.data_root,
            args.output,
            args.model,
            args.use_llm,
            args.prediction_prefix,
            args.max_events,
            detector=args.detector,
            decision_backend=args.decision_backend,
            api_base=args.api_base,
            api_key_env=args.api_key_env,
            config_path=args.config_path,
            inference_log=args.inference_log,
        )
    except Exception as exc:
        print(f"Baseline failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

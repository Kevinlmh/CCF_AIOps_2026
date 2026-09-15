"""Run the multi-source hybrid AIOps diagnosis pipeline."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
METRIC_SEMANTICS_PATH = HERE / "config" / "metric_semantics.json"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from aiops_challenge_2026.config import load_public_config
from aiops_challenge_2026.schema import validate_prediction
from baseline.bian.checkpoint import (
    load_event_checkpoint,
    load_evidence_checkpoint,
    save_event_checkpoint,
    save_evidence_checkpoint,
)
from baseline.bian.anomaly_detector.five_sigma import detect as detect_five_sigma
from baseline.bian.anomaly_detector.event_clustering import split_concurrent_events
from baseline.bian.anomaly_detector.robust_detector import (
    DetectionDiagnostics,
    detect_events_with_evidence,
    segment_evidence,
)
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
from baseline.bian.models.api_backend import ApiBackend, ApiConfig, resolve_api_base
from baseline.bian.models.backend import JsonModelBackend, ModelConfig
from baseline.bian.preprocessing.evidence import public_topology
from baseline.bian.preprocessing.multisource import (
    SOURCE_ORDER,
    ObservationBundle,
    iter_source_files,
    load_observations,
)
from baseline.bian.preprocessing.observations import (
    AnomalyEvidence,
    DetectedEvent,
    parse_time,
)
from baseline.bian.preprocessing.metric_semantics import (
    MetricSemantics,
    transform_observations,
)
from baseline.bian.preprocessing.streaming import detect_events_streaming


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


def _pipeline_fingerprint(
    model_config: dict[str, Any],
    semantics_path: Path = METRIC_SEMANTICS_PATH,
    *,
    implementation_paths: tuple[Path, ...] | None = None,
) -> str:
    """Hash every rule that affects the reusable statistical event cache."""
    semantics = json.loads(semantics_path.read_text(encoding="utf-8"))
    canonical = json.dumps(
        {"model": model_config, "metric_semantics": semantics},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical)
    paths = implementation_paths or (
        HERE / "anomaly_detector" / "robust_detector.py",
        HERE / "anomaly_detector" / "streaming_detector.py",
        HERE / "anomaly_detector" / "event_clustering.py",
        HERE / "preprocessing" / "metric_semantics.py",
        HERE / "preprocessing" / "multisource.py",
        HERE / "preprocessing" / "observations.py",
        HERE / "preprocessing" / "streaming.py",
    )
    for path in sorted(paths, key=lambda item: item.as_posix()):
        try:
            label = path.resolve().relative_to(REPO.resolve()).as_posix()
        except ValueError:
            label = path.name
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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


def _select_llm_candidates(
    candidates: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Keep deterministic Top5, then the strongest evidenced candidates."""
    maximum = max(5, int(limit))
    mandatory = list(candidates[:5])
    selected_ids = {item["node_id"] for item in mandatory}
    evidenced = [
        item
        for item in candidates[5:]
        if item.get("anomaly_count", 0) > 0 or item.get("related_anomaly_count", 0) > 0
    ]
    fallback = [item for item in candidates[5:] if item not in evidenced]
    result = mandatory[:maximum]
    for item in evidenced + fallback:
        if len(result) >= maximum:
            break
        if item["node_id"] not in selected_ids:
            result.append(item)
            selected_ids.add(item["node_id"])
    return result


def _ordered_parallel_map(function, values, *, workers: int):
    """Run independent API-bound work concurrently without reordering output."""
    if workers <= 1:
        return [function(value) for value in values]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(function, values))


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
    selected = _select_llm_candidates(
        ranking.candidates,
        limit=int(config["model"].get("llm_candidate_limit", 12)),
    )
    selected_nodes = {candidate["node_id"] for candidate in selected}
    for candidate in selected:
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
            "semantic_score": round(point.semantic_score, 5),
            "event_role": point.event_role,
        }
        for point in sorted(event.evidence, key=lambda item: (item.timestamp, -item.score))
        if point.node_id in selected_nodes
    ]
    timeline = timeline[:limit]
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
    ingestion_mode: str = "memory",
    streaming_metrics: dict[str, int] | None = None,
    cached_source_coverage: dict[str, Any] | None = None,
    model_version: str = "unknown",
) -> dict[str, Any]:
    nonzero_energy: list[dict[str, Any]] = []
    energy_summary: dict[str, Any] = {}
    if diagnostics is not None:
        all_nonzero = [
            {"timestamp_utc": _utc(minute), "energy": round(energy, 5)}
            for minute, energy in diagnostics.minute_energy.items()
            if energy > 0
        ]
        trigger_values = list(diagnostics.trigger_minute_energy.values())
        trigger_nonzero = [value for value in trigger_values if value > 0]
        total_minutes = len(diagnostics.minute_energy)
        observation_minutes = set(diagnostics.minute_energy)
        covered_minutes = set()
        for event in event_logs:
            start = parse_time(event.get("start_time"))
            end = parse_time(event.get("end_time"))
            if start is None or end is None:
                continue
            current = start.replace(second=0, microsecond=0)
            while current < end:
                if current in observation_minutes:
                    covered_minutes.add(current)
                current += timedelta(minutes=1)
        nonzero_energy = sorted(
            all_nonzero,
            key=lambda item: (-item["energy"], item["timestamp_utc"]),
        )[:4096]
        energy_summary = {
            "total_minutes": total_minutes,
            "nonzero_minutes": len(all_nonzero),
            "nonzero_minute_ratio": round(
                len(all_nonzero) / total_minutes, 8
            ) if total_minutes else 0.0,
            "trigger_nonzero_minutes": len(trigger_nonzero),
            "trigger_nonzero_minute_ratio": round(
                len(trigger_nonzero) / total_minutes, 8
            ) if total_minutes else 0.0,
            "event_covered_minutes": len(covered_minutes),
            "event_coverage_ratio": round(
                len(covered_minutes) / total_minutes, 8
            ) if total_minutes else 0.0,
            "trigger_minutes_above_open_threshold": sum(
                value >= diagnostics.open_threshold for value in trigger_values
            ),
            "maximum_trigger_energy": round(max(trigger_values, default=0.0), 5),
            "mean_nonzero_trigger_energy": round(
                sum(trigger_nonzero) / len(trigger_nonzero), 5
            ) if trigger_nonzero else 0.0,
            "minute_energy_sample_policy": "top_energy_then_timestamp",
            "minute_energy_sample_size": len(nonzero_energy),
        }
    coverage = (
        {source: bundle.source_coverage.get(source, 0) for source in SOURCE_ORDER}
        if bundle is not None
        else {
            source: diagnostics.source_coverage.get(source, 0)
            for source in SOURCE_ORDER
        }
        if diagnostics is not None and diagnostics.source_coverage
        else {
            source: int((cached_source_coverage or {}).get(source, 0))
            for source in SOURCE_ORDER
        }
    )
    stats = bundle.stats if bundle is not None else None
    data_audit = {
        "files": dict(stats.files_by_source) if stats is not None else {},
        "rows_read": dict(stats.rows_by_source) if stats is not None else {},
        "valid_rows": dict(stats.valid_rows_by_source) if stats is not None else {},
        "filtered_rows": dict(stats.filtered_rows_by_source) if stats is not None else {},
        "invalid_rows": dict(stats.bad_rows_by_source) if stats is not None else {},
        "emitted_observations": dict(stats.emitted_by_source) if stats is not None else {},
        "unknown_nodes": dict(stats.unknown_nodes_by_source) if stats is not None else {},
        "time_ranges": {
            source: {
                "start_time": _utc(start),
                "end_time": _utc(stats.last_timestamp_by_source[source]),
            }
            for source, start in stats.first_timestamp_by_source.items()
        } if stats is not None else {},
        "rows_conserved": {
            source: stats.rows_conserved(source)
            for source in stats.rows_by_source
        } if stats is not None else {},
        **(streaming_metrics or {}),
    }
    return {
        "model_version": model_version,
        "detector": detector_name,
        "backend": backend_name,
        "ingestion_mode": ingestion_mode,
        "source_coverage": coverage,
        "bad_rows": dict(bundle.stats.bad_rows_by_source) if bundle is not None else {},
        "warnings": list(bundle.stats.warnings[:100]) if bundle is not None else [],
        "evidence_count": diagnostics.evidence_count if diagnostics is not None else None,
        "energy_summary": energy_summary,
        "minute_energy_nonzero": nonzero_energy,
        "data_audit": data_audit,
        "events": event_logs,
    }


def _localized_event_bounds(
    event: DetectedEvent,
    root_node_id: str | None,
    config: dict[str, Any],
) -> tuple[datetime, datetime]:
    """Bound an incident by direct root triggers, not unrelated global tails."""
    if not root_node_id:
        return event.start, event.end
    direct = [
        point
        for point in event.evidence
        if point.event_role == "trigger" and point.node_id == root_node_id
    ]
    if not direct:
        return event.start, event.end
    gap = max(0, int(config.get("localized_gap_tolerance_minutes", 1)))
    by_minute: dict[datetime, list[AnomalyEvidence]] = {}
    for point in direct:
        minute = point.timestamp.replace(second=0, microsecond=0)
        by_minute.setdefault(minute, []).append(point)
    clusters: list[list[datetime]] = []
    for minute in sorted(by_minute):
        if not clusters or minute - clusters[-1][-1] > timedelta(minutes=gap + 1):
            clusters.append([minute])
        else:
            clusters[-1].append(minute)

    def cluster_quality(cluster: list[datetime]) -> tuple[float, ...]:
        points = [point for minute in cluster for point in by_minute[minute]]
        minute_strength = sum(
            max(point.score for point in by_minute[minute]) for minute in cluster
        )
        return (
            float(len(cluster)),
            max((point.semantic_score for point in points), default=0.0),
            minute_strength,
            -cluster[0].timestamp(),
        )

    primary = max(clusters, key=cluster_quality)
    start_padding = max(0, int(config.get("localized_start_padding_minutes", 1)))
    end_padding = max(1, int(config.get("localized_end_padding_minutes", 1)))
    first, last = primary[0], primary[-1]
    localized_start = max(
        event.start,
        first - timedelta(minutes=start_padding),
    )
    localized_end = min(event.end, last + timedelta(minutes=end_padding))
    maximum = timedelta(minutes=max(1, int(config.get("max_event_minutes", 30))))
    if localized_end - localized_start > maximum:
        localized_end = localized_start + maximum
    return localized_start, localized_end


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
    ingestion_mode: str = "auto",
    scratch_dir: Path | None = None,
    strict_input: bool = True,
    spatial_split: bool = True,
    api_timeout: float = 120.0,
    event_cache: Path | None = None,
    reuse_event_cache: bool = False,
    llm_workers: int = 1,
    evidence_cache: Path | None = None,
    reuse_evidence_cache: bool = False,
) -> int:
    config = _config()
    model_config = _model_config(config_path)
    pipeline_fingerprint = _pipeline_fingerprint(
        model_config,
        METRIC_SEMANTICS_PATH,
    )
    network = load_public_config("network_elements")
    taxonomy = load_public_config("fault_taxonomy")
    topology = public_topology(config)

    if detector not in {"robust", "five-sigma"}:
        raise ValueError(f"unsupported detector: {detector}")
    bundle: ObservationBundle | None = None
    diagnostics: DetectionDiagnostics | None = None
    streaming_metrics: dict[str, int] = {}
    cache_metadata: dict[str, Any] = {}
    if reuse_event_cache and reuse_evidence_cache:
        raise ValueError("event and evidence checkpoints cannot both be reused")
    selected_ingestion = (
        "checkpoint"
        if reuse_event_cache
        else "evidence_checkpoint"
        if reuse_evidence_cache
        else ingestion_mode
    )
    evidence_points: tuple[AnomalyEvidence, ...] = ()
    if reuse_evidence_cache:
        if evidence_cache is None:
            raise ValueError("--reuse-evidence-cache requires --evidence-cache")
        (
            evidence_points,
            (observation_start, observation_end),
            cached_coverage,
            cache_metadata,
        ) = load_evidence_checkpoint(evidence_cache)
        cached_model_version = cache_metadata.get("model_version")
        current_model_version = model_config.get("version", "unknown")
        if cached_model_version != current_model_version:
            raise ValueError(
                "evidence checkpoint model version differs from the active config: "
                f"cache={cached_model_version!r}, active={current_model_version!r}"
            )
        if cache_metadata.get("pipeline_fingerprint") != pipeline_fingerprint:
            raise ValueError(
                "evidence checkpoint pipeline fingerprint differs from the active pipeline"
            )
        events, diagnostics = segment_evidence(
            evidence_points,
            observation_start=observation_start,
            observation_end=observation_end,
            config=model_config["detector"],
            source_coverage=cached_coverage,
        )
        if spatial_split:
            events = split_concurrent_events(events, network["cities"])
    elif reuse_event_cache:
        if event_cache is None:
            raise ValueError("--reuse-event-cache requires --event-cache")
        events, cache_metadata = load_event_checkpoint(event_cache)
        cached_model_version = cache_metadata.get("model_version")
        current_model_version = model_config.get("version", "unknown")
        if cached_model_version != current_model_version:
            raise ValueError(
                "event checkpoint model version differs from the active config: "
                f"cache={cached_model_version!r}, active={current_model_version!r}"
            )
        cached_fingerprint = cache_metadata.get("pipeline_fingerprint")
        if cached_fingerprint != pipeline_fingerprint:
            raise ValueError(
                "event checkpoint pipeline fingerprint differs from the active "
                "model or metric semantics"
            )
        saved_metrics = cache_metadata.get("streaming_metrics", {})
        if isinstance(saved_metrics, dict):
            streaming_metrics = {
                str(key): int(value)
                for key, value in saved_metrics.items()
                if isinstance(value, (int, float))
            }
    else:
        if selected_ingestion == "auto":
            formal_layout = any(
                path.parent.name.lower().endswith("_data")
                for _, path in iter_source_files(data_root)
            )
            selected_ingestion = "streaming" if formal_layout else "memory"
        if selected_ingestion not in {"memory", "streaming"}:
            raise ValueError(f"unsupported ingestion mode: {selected_ingestion}")
        if detector == "robust" and selected_ingestion == "streaming":
            formal_layout = any(
                path.parent.name.lower().endswith("_data")
                for _, path in iter_source_files(data_root)
            )
            streamed = detect_events_streaming(
                data_root,
                config["region_aliases"],
                network["device_roles"],
                model_config["detector"],
                semantics_path=METRIC_SEMANTICS_PATH,
                scratch_dir=scratch_dir,
                strict_cities=(network["cities"] if strict_input and formal_layout else None),
            )
            bundle = streamed.bundle
            diagnostics = streamed.diagnostics
            evidence_points = streamed.evidence
            events = list(streamed.events)
            streaming_metrics = {
                "observations_evaluated": streamed.observation_count,
                "series_state_count": streamed.series_state_count,
                "dropped_evidence": streamed.dropped_evidence_count,
            }
            if spatial_split:
                events = split_concurrent_events(events, network["cities"])
        elif detector == "robust":
            bundle = load_observations(
                data_root,
                config["region_aliases"],
                network["device_roles"],
            )
            semantics = MetricSemantics.from_json(METRIC_SEMANTICS_PATH)
            bundle = ObservationBundle(
                tuple(transform_observations(bundle.numeric, semantics)),
                bundle.text_events,
                bundle.stats,
                bundle.source_coverage,
            )
            events, diagnostics, evidence_points = detect_events_with_evidence(
                bundle, model_config["detector"]
            )
        else:
            events = _legacy_events(data_root, config["region_aliases"])
        if evidence_cache is not None:
            if detector != "robust" or diagnostics is None:
                raise ValueError("evidence checkpoints require the robust detector")
            if diagnostics.observation_start is None or diagnostics.observation_end is None:
                raise ValueError("cannot save evidence checkpoint without observation bounds")
            save_evidence_checkpoint(
                evidence_points,
                evidence_cache,
                observation_start=diagnostics.observation_start,
                observation_end=diagnostics.observation_end,
                source_coverage=diagnostics.source_coverage,
                metadata={
                    "dataset_root": str(data_root.resolve()),
                    "model_version": model_config.get("version", "unknown"),
                    "pipeline_fingerprint": pipeline_fingerprint,
                    "ingestion_mode": selected_ingestion,
                },
            )
    if event_cache is not None and not reuse_event_cache:
        coverage = (
            diagnostics.source_coverage
            if diagnostics is not None
            else bundle.source_coverage
            if bundle is not None
            else {}
        )
        save_event_checkpoint(
            events,
            event_cache,
            metadata={
                "dataset_root": cache_metadata.get(
                    "dataset_root", str(data_root.resolve())
                ),
                "model_version": model_config.get("version", "unknown"),
                "pipeline_fingerprint": pipeline_fingerprint,
                "ingestion_mode": selected_ingestion,
                "source_coverage": coverage,
                "streaming_metrics": streaming_metrics,
            },
        )
    if max_events is not None:
        events = events[:max_events]

    backend_name = "transformers" if use_llm and decision_backend == "local" else decision_backend
    if backend_name not in {"local", "transformers", "api"}:
        raise ValueError(f"unsupported decision backend: {backend_name}")
    backend: Any = None
    if backend_name == "transformers":
        backend = JsonModelBackend(
            model,
            ModelConfig.from_mapping(config["model"]),
            HERE / "prompts",
        )
    elif backend_name == "api":
        resolved_api_base = resolve_api_base(api_base)
        if not resolved_api_base:
            raise ValueError(
                "--api-base or AIOPS_LLM_API_BASE is required when --decision-backend api"
            )
        backend = ApiBackend(
            ApiConfig(
                base_url=resolved_api_base,
                model=model,
                api_key_env=api_key_env,
                timeout=max(1.0, api_timeout),
                retries=config["model"]["retries"],
            ),
            HERE / "prompts",
        )

    def diagnose(index_event: tuple[int, DetectedEvent]):
        index, event = index_event
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
        root_node_id = top5[0]["network_element_id"] if top5 else None
        localized_start, localized_end = _localized_event_bounds(
            event, root_node_id, model_config["detector"]
        )
        record = {
            "prediction_id": f"{prediction_prefix}{index:06d}",
            "start_time": _utc(localized_start),
            "end_time": _utc(localized_end),
            "root_cause_top5": top5,
            "fault_category": category,
        }
        event_log = {
            "prediction_id": f"{prediction_prefix}{index:06d}",
            "start_time": _utc(localized_start),
            "end_time": _utc(localized_end),
            "detected_start_time": _utc(event.start),
            "detected_end_time": _utc(event.end),
            "peak_time": _utc(event.peak_time),
            "confidence": event.confidence,
            "source_counts": event.source_counts,
            "candidates": list(ranking.candidates[:10]),
            "prototype_top3": list(prototype.top3),
            "prototype_signals": prototype.signals,
            "candidate_scope": ranking.scope,
        }
        return record, event_log

    indexed_events = list(enumerate(events, 1))
    workers = max(1, int(llm_workers)) if backend_name == "api" else 1
    diagnosed = _ordered_parallel_map(diagnose, indexed_events, workers=workers)
    records = [record for record, _ in diagnosed]
    event_logs = [event_log for _, event_log in diagnosed]
    _write_predictions_atomic(records, output)
    if inference_log is not None:
        _write_json_atomic(
            _diagnostic_log(
                bundle=bundle,
                diagnostics=diagnostics,
                detector_name=detector,
                backend_name=backend_name,
                event_logs=event_logs,
                ingestion_mode=selected_ingestion,
                streaming_metrics=streaming_metrics,
                cached_source_coverage=(
                    cache_metadata.get("source_coverage", {})
                    if reuse_event_cache
                    else None
                ),
                model_version=str(model_config.get("version", "unknown")),
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
    parser.add_argument("--api-timeout", type=float, default=120.0)
    parser.add_argument("--config-path", type=Path)
    parser.add_argument("--inference-log", type=Path)
    parser.add_argument(
        "--ingestion-mode", choices=("auto", "memory", "streaming"), default="auto"
    )
    parser.add_argument("--scratch-dir", type=Path)
    parser.add_argument(
        "--allow-partial-input",
        action="store_true",
        help="allow a formal *_data tree that does not contain all eight cities",
    )
    parser.add_argument("--disable-spatial-split", action="store_true")
    parser.add_argument(
        "--event-cache",
        type=Path,
        help="save/load anomaly events between CPU preprocessing and LLM inference",
    )
    parser.add_argument("--reuse-event-cache", action="store_true")
    parser.add_argument(
        "--evidence-cache",
        type=Path,
        help="save/load bounded anomaly evidence before event segmentation",
    )
    parser.add_argument("--reuse-evidence-cache", action="store_true")
    parser.add_argument(
        "--llm-workers",
        type=int,
        default=1,
        help="parallel event workers for the OpenAI-compatible API backend",
    )
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
            ingestion_mode=args.ingestion_mode,
            scratch_dir=args.scratch_dir,
            strict_input=not args.allow_partial_input,
            spatial_split=not args.disable_spatial_split,
            api_timeout=args.api_timeout,
            event_cache=args.event_cache,
            reuse_event_cache=args.reuse_event_cache,
            llm_workers=args.llm_workers,
            evidence_cache=args.evidence_cache,
            reuse_evidence_cache=args.reuse_evidence_cache,
        )
    except Exception as exc:
        print(f"Baseline failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

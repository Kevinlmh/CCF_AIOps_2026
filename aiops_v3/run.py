"""End-to-end Mac diagnosis with optional server response import."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from .contracts import load_contract
from .calibration import load_calibration
from .detection import DetectorSettings, detect_with_audit
from .diagnosis import build_evidence, diagnose_rules
from .optional_llm import choose_diagnosis, load_diagnoses
from .role_import import apply_roles, load_roles
from .output import prediction_record
from .raw import build_sample_store
from .store import open_store


@dataclass(frozen=True)
class RunSummary:
    event_count: int
    fallback_count: int
    output_dir: Path


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_summary(event, store, top_root: str) -> dict:
    direct = [signal for signal in event.signals if signal.role == "direct"]
    symptoms = [signal for signal in event.signals if signal.role == "symptom"]
    direct_nodes = {signal.node for signal in direct}
    direct_features = {signal.feature for signal in direct}
    probes = {
        store.edges[int(signal.evidence_id.split(":", 3)[2])]["source"]
        for signal in symptoms if signal.evidence_id.startswith("edge:")
    }
    if len(direct_nodes) > 1:
        tier = "multi_node_direct"
    elif len(direct_features) > 1:
        tier = "multi_metric_direct"
    elif direct:
        tier = "single_metric_direct"
    elif len(probes) > 1:
        tier = "multi_probe_symptom"
    else:
        tier = "single_probe_symptom"
    return {
        "evidence_tier": tier,
        "direct_nodes": sorted(direct_nodes),
        "symptom_targets": sorted({signal.node for signal in symptoms}),
        "symptom_probe_count": len(probes),
        "root_has_direct_evidence": top_root in direct_nodes,
    }


def run(
    input_store: Path,
    output_dir: Path,
    mode: str = "rules",
    llm_responses: Path | None = None,
    detector_profile: str = "standard",
    request_aware_service: bool = False,
    source_aware_candidates: bool = False,
    baseline_strategy: str = "global_median",
    service_overlap_policy: str = "city",
    routing_context_candidates: bool = False,
    temporal_context: bool = False,
    routing_dimension_detection: bool = False,
    cross_city_merge_policy: str = "correlated",
    calibration_profile: Path | None = None,
    exact_bgp_session_merge: bool = False,
    prefix_aware_blackhole: bool = False,
    netflow_stats: Path | None = None,
    netflow_events: bool = False,
    keep_weak_cpu: bool = False,
    role_results: Path | None = None,
    role_scope: str = "all",
    boundary_refine: bool = True,
) -> RunSummary:
    if mode not in {"rules", "llm-jsonl", "roles-jsonl"}:
        raise ValueError(f"unknown diagnosis mode: {mode}")
    if mode == "llm-jsonl" and llm_responses is None:
        raise ValueError("llm-jsonl mode requires response file")
    if mode == "roles-jsonl" and role_results is None:
        raise ValueError("roles-jsonl mode requires role response file")
    if netflow_events and netflow_stats is None:
        raise ValueError("netflow_events requires netflow_stats")
    if role_scope not in {"all", "ambiguous"}:
        raise ValueError(f"unknown role scope: {role_scope}")
    if detector_profile not in {"standard", "conservative"}:
        raise ValueError(f"unknown detector profile: {detector_profile}")
    if baseline_strategy not in {"global_median", "healthy_tail", "rolling_healthy_tail"}:
        raise ValueError(f"unknown baseline strategy: {baseline_strategy}")
    if service_overlap_policy not in {"city", "related_device"}:
        raise ValueError(f"unknown service overlap policy: {service_overlap_policy}")
    if cross_city_merge_policy not in {"correlated", "same_city"}:
        raise ValueError(f"unknown cross-city merge policy: {cross_city_merge_policy}")
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output_dir}")
    store = open_store(Path(input_store))
    contract = load_contract()
    rule_overrides = (load_calibration(calibration_profile, input_store)
                      if calibration_profile is not None else {})
    calibration_status = (json.loads(Path(calibration_profile).read_text(encoding="utf-8"))["status"]
                          if calibration_profile is not None else None)
    settings = (DetectorSettings(weak_cpu_max_minutes=3, weak_cpu_max_score=8,
                                 request_aware_service=request_aware_service,
                                 baseline_strategy=baseline_strategy,
                                 service_overlap_policy=service_overlap_policy,
                                 routing_dimension_detection=routing_dimension_detection,
                                 cross_city_merge_policy=cross_city_merge_policy,
                                 exact_bgp_session_merge=exact_bgp_session_merge,
                                 prefix_aware_blackhole=prefix_aware_blackhole,
                                 netflow_events=netflow_events,
                                 netflow_stats_path=str(netflow_stats) if netflow_stats else None,
                                 keep_weak_cpu=keep_weak_cpu,
                                 rule_overrides=rule_overrides)
                if detector_profile == "conservative"
                else DetectorSettings(request_aware_service=request_aware_service,
                                      baseline_strategy=baseline_strategy,
                                      service_overlap_policy=service_overlap_policy,
                                      routing_dimension_detection=routing_dimension_detection,
                                      cross_city_merge_policy=cross_city_merge_policy,
                                      exact_bgp_session_merge=exact_bgp_session_merge,
                                      prefix_aware_blackhole=prefix_aware_blackhole,
                                      netflow_events=netflow_events,
                                      netflow_stats_path=str(netflow_stats) if netflow_stats else None,
                                      keep_weak_cpu=keep_weak_cpu,
                                      rule_overrides=rule_overrides))
    detection = detect_with_audit(store, settings)
    responses = load_diagnoses(llm_responses) if mode == "llm-jsonl" else {}
    role_rows = load_roles(role_results) if mode == "roles-jsonl" else {}
    predictions: list[dict] = []
    evidence: list[dict] = []
    audit: list[dict] = []
    fallback_count = 0
    for event in detection.events:
        pack = build_evidence(store, event, contract,
                              include_probe_candidates=source_aware_candidates,
                              include_routing_context=routing_context_candidates,
                              include_temporal_context=temporal_context)
        rules = diagnose_rules(pack, contract)
        boundary = None
        role_fallbacks: list[str] = []
        if mode == "llm-jsonl":
            diagnosis, reason = choose_diagnosis(pack, rules, responses.get(event.event_id), contract)
            fallback_count += reason is not None
        elif mode == "roles-jsonl":
            diagnosis, boundary, role_fallbacks = apply_roles(
                pack, rules, role_rows.get(event.event_id, {}), contract)
            if not boundary_refine:
                boundary = None
            fallback_count += bool(role_fallbacks)
            reason = None
        else:
            diagnosis, reason = rules, None
        predictions.append(prediction_record(
            pack, diagnosis, contract,
            start_time=boundary["start_time"] if boundary else None,
            end_time=boundary["end_time"] if boundary else None))
        evidence_record = asdict(pack)
        for name in ("temporal_context", "traffic_observations", "observers",
                     "topology", "candidate_priors", "evidence_owners", "quality"):
            if not evidence_record.get(name):
                evidence_record.pop(name, None)
        evidence.append({**evidence_record, "evidence_sha256": pack.sha256()})
        audit.append({
            "event_id": event.event_id,
            **_evidence_summary(event, store, diagnosis.roots[0]),
            "diagnosis_source": diagnosis.source,
            "fallback_reason": reason,
            "evidence_ids": diagnosis.evidence_ids,
            "candidate_count": len(pack.candidates),
            "signal_count": len(pack.signals),
            "model": responses.get(event.event_id, {}).get("model") if mode == "llm-jsonl" else None,
            "role_fallback_fields": role_fallbacks,
            "role_boundary": boundary,
        })
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "predictions.jsonl", predictions)
    _write_jsonl(output_dir / "evidence.jsonl", evidence)
    _write_jsonl(output_dir / "audit.jsonl", audit)
    manifest_bytes = (Path(input_store) / "manifest.json").read_bytes()
    package_dir = Path(__file__).parent
    summary = {
        "format_version": 1,
        "mode": mode,
        "detector_profile": detector_profile,
        "source_aware_candidates": source_aware_candidates,
        "routing_context_candidates": routing_context_candidates,
        "temporal_context": temporal_context,
        "input_store": str(input_store),
        "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "input_array_sha256": {
            f"{kind}_{item}": _sha256(Path(input_store) / f"{kind}_{item}.npy")
            for kind in ("node", "edge", "log") for item in ("values", "mask")
        },
        "input_sidecar_sha256": {
            name: _sha256(Path(input_store) / name)
            for name in ("dimension_series.json", "dimension_cells.npy", "text_evidence.jsonl")
            if (Path(input_store) / name).is_file()
        },
        "llm_responses_sha256": _sha256(llm_responses) if llm_responses else None,
        "role_results_sha256": _sha256(role_results) if role_results else None,
        "role_scope": role_scope if mode == "roles-jsonl" else None,
        "netflow_stats_sha256": _sha256(netflow_stats) if netflow_stats else None,
        "netflow_events": netflow_events,
        "code_sha256": {
            str(path.relative_to(package_dir)): _sha256(path)
            for path in sorted((*package_dir.rglob("*.py"), *package_dir.glob("config/*.json")))
        },
        "output_sha256": {
            name: _sha256(output_dir / name)
            for name in ("predictions.jsonl", "evidence.jsonl", "audit.jsonl")
        },
        "source_counts": store.manifest.get("source_counts", {}),
        "source_profile": store.manifest.get("source_profile", "unknown"),
        "calibration_profile_sha256": _sha256(calibration_profile) if calibration_profile else None,
        "calibration_status": calibration_status,
        "detector_settings": asdict(settings),
        "event_count": len(predictions),
        "fallback_count": fallback_count,
        "rejected_events": list(detection.rejected),
        "merged_events": list(detection.merged),
    }
    (output_dir / "run_manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return RunSummary(len(predictions), fallback_count, output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run v3 without local LLM weights")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-store", type=Path)
    source.add_argument("--raw-root", type=Path)
    parser.add_argument("--build-store-to", type=Path)
    parser.add_argument("--source-profile", choices=("stage1", "stage2"), default="stage1")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--diagnoser", choices=("rules", "llm-jsonl", "roles-jsonl"), default="rules")
    parser.add_argument("--detector-profile", choices=("standard", "conservative"), default="standard")
    parser.add_argument("--request-aware-service", action="store_true")
    parser.add_argument("--source-aware-candidates", action="store_true")
    parser.add_argument("--baseline-strategy", choices=("global_median", "healthy_tail", "rolling_healthy_tail"),
                        default="global_median")
    parser.add_argument("--service-overlap-policy", choices=("city", "related_device"),
                        default="city")
    parser.add_argument("--routing-context-candidates", action="store_true")
    parser.add_argument("--temporal-context", action="store_true")
    parser.add_argument("--routing-dimension-detection", action="store_true")
    parser.add_argument("--cross-city-merge-policy", choices=("correlated", "same_city"),
                        default="correlated")
    parser.add_argument("--exact-bgp-session-merge", action="store_true")
    parser.add_argument("--prefix-aware-blackhole", action="store_true",
                        help="Require a same-prefix observed presence-to-absence onset")
    parser.add_argument("--llm-responses", type=Path)
    parser.add_argument("--netflow-stats", type=Path)
    parser.add_argument("--netflow-events", action="store_true")
    parser.add_argument("--keep-weak-cpu", action="store_true")
    parser.add_argument("--role-results", type=Path)
    parser.add_argument("--role-scope", choices=("all", "ambiguous"), default="all")
    parser.add_argument("--no-boundary-refine", action="store_true")
    parser.add_argument("--calibration-profile", type=Path)
    args = parser.parse_args()
    if args.raw_root and not args.build_store_to:
        parser.error("--raw-root requires --build-store-to")
    store = (build_sample_store(args.raw_root, args.build_store_to, profile=args.source_profile)
             if args.raw_root else args.input_store)
    summary = run(store, args.output_dir, args.diagnoser, args.llm_responses,
                  args.detector_profile, args.request_aware_service,
                  args.source_aware_candidates, args.baseline_strategy,
                  args.service_overlap_policy, args.routing_context_candidates,
                  args.temporal_context, args.routing_dimension_detection,
                  args.cross_city_merge_policy, args.calibration_profile,
                  args.exact_bgp_session_merge, args.prefix_aware_blackhole,
                  args.netflow_stats, args.netflow_events, args.keep_weak_cpu,
                  args.role_results, args.role_scope, not args.no_boundary_refine)
    print(json.dumps(asdict(summary), default=str))


if __name__ == "__main__":
    main()

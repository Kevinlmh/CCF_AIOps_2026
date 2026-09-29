"""End-to-end Mac diagnosis with optional server response import."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from .contracts import load_contract
from .detection import DetectorSettings, detect_with_audit
from .diagnosis import build_evidence, diagnose_rules
from .optional_llm import choose_diagnosis, load_diagnoses
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
) -> RunSummary:
    if mode not in {"rules", "llm-jsonl"}:
        raise ValueError(f"unknown diagnosis mode: {mode}")
    if mode == "llm-jsonl" and llm_responses is None:
        raise ValueError("llm-jsonl mode requires response file")
    if detector_profile not in {"standard", "conservative"}:
        raise ValueError(f"unknown detector profile: {detector_profile}")
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output_dir}")
    store = open_store(Path(input_store))
    contract = load_contract()
    settings = (DetectorSettings(weak_cpu_max_minutes=3, weak_cpu_max_score=8)
                if detector_profile == "conservative" else DetectorSettings())
    detection = detect_with_audit(store, settings)
    responses = load_diagnoses(llm_responses) if mode == "llm-jsonl" else {}
    predictions: list[dict] = []
    evidence: list[dict] = []
    audit: list[dict] = []
    fallback_count = 0
    for event in detection.events:
        pack = build_evidence(store, event, contract)
        rules = diagnose_rules(pack, contract)
        if mode == "llm-jsonl":
            diagnosis, reason = choose_diagnosis(pack, rules, responses.get(event.event_id), contract)
            fallback_count += reason is not None
        else:
            diagnosis, reason = rules, None
        predictions.append(prediction_record(pack, diagnosis, contract))
        evidence.append({**asdict(pack), "evidence_sha256": pack.sha256()})
        audit.append({
            "event_id": event.event_id,
            **_evidence_summary(event, store, diagnosis.roots[0]),
            "diagnosis_source": diagnosis.source,
            "fallback_reason": reason,
            "evidence_ids": diagnosis.evidence_ids,
            "candidate_count": len(pack.candidates),
            "signal_count": len(pack.signals),
            "model": responses.get(event.event_id, {}).get("model") if mode == "llm-jsonl" else None,
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
        "input_store": str(input_store),
        "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "input_array_sha256": {
            f"{kind}_{item}": _sha256(Path(input_store) / f"{kind}_{item}.npy")
            for kind in ("node", "edge", "log") for item in ("values", "mask")
        },
        "llm_responses_sha256": _sha256(llm_responses) if llm_responses else None,
        "code_sha256": {
            str(path.relative_to(package_dir)): _sha256(path)
            for path in sorted((*package_dir.glob("*.py"), *package_dir.glob("config/*.json")))
        },
        "output_sha256": {
            name: _sha256(output_dir / name)
            for name in ("predictions.jsonl", "evidence.jsonl", "audit.jsonl")
        },
        "source_counts": store.manifest.get("source_counts", {}),
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
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--diagnoser", choices=("rules", "llm-jsonl"), default="rules")
    parser.add_argument("--detector-profile", choices=("standard", "conservative"), default="standard")
    parser.add_argument("--llm-responses", type=Path)
    args = parser.parse_args()
    if args.raw_root and not args.build_store_to:
        parser.error("--raw-root requires --build-store-to")
    store = build_sample_store(args.raw_root, args.build_store_to) if args.raw_root else args.input_store
    summary = run(store, args.output_dir, args.diagnoser, args.llm_responses, args.detector_profile)
    print(json.dumps(asdict(summary), default=str))


if __name__ == "__main__":
    main()

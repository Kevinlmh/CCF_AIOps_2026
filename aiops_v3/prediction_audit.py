"""Verify two batch runs and their merged official prediction file."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .contracts import load_contract, parse_time, validate_prediction
from .merge import merge_predictions
from .server_llm import _pack


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _overlapping_same_category(rows: list[dict]) -> list[dict]:
    ordered = sorted(rows, key=lambda row: parse_time(row["start_time"]))
    overlapping = []
    for index, first in enumerate(ordered):
        start_first, end_first = parse_time(first["start_time"]), parse_time(first["end_time"])
        for second in ordered[index + 1:]:
            start_second = parse_time(second["start_time"])
            if start_second >= end_first:
                break
            if first["fault_category"] != second["fault_category"]:
                continue
            end_second = parse_time(second["end_time"])
            shared = (min(end_first, end_second) - max(start_first, start_second)).total_seconds()
            dice = 2 * shared / ((end_first - start_first).total_seconds()
                                 + (end_second - start_second).total_seconds())
            if dice >= .4:
                overlapping.append({"first": first["prediction_id"],
                                    "second": second["prediction_id"],
                                    "dice": round(dice, 3)})
    return overlapping


def _audit_run(directory: Path) -> tuple[dict, list[dict]]:
    directory = Path(directory)
    manifest = json.loads((directory / "run_manifest.json").read_text(encoding="utf-8"))
    source = Path(manifest["input_store"])
    for name, expected in manifest["output_sha256"].items():
        if _sha256(directory / name) != expected:
            raise ValueError(f"{directory / name} hash differs from run manifest")
    if _sha256(source / "manifest.json") != manifest["input_manifest_sha256"]:
        raise ValueError(f"{source / 'manifest.json'} hash differs from run manifest")
    for name, expected in manifest.get("input_array_sha256", {}).items():
        if _sha256(source / f"{name}.npy") != expected:
            raise ValueError(f"{source / (name + '.npy')} hash differs from run manifest")
    for name, expected in manifest.get("input_sidecar_sha256", {}).items():
        if _sha256(source / name) != expected:
            raise ValueError(f"{source / name} hash differs from run manifest")
    package_dir = Path(__file__).parent
    for name, expected in manifest.get("code_sha256", {}).items():
        if _sha256(package_dir / name) != expected:
            raise ValueError(f"{package_dir / name} code hash differs from run manifest")

    predictions = _read_jsonl(directory / "predictions.jsonl")
    evidence = _read_jsonl(directory / "evidence.jsonl")
    audit = _read_jsonl(directory / "audit.jsonl")
    if len(predictions) != len(evidence) or len(predictions) != len(audit):
        raise ValueError(f"{directory}: prediction, evidence and audit row counts differ")
    if len(predictions) != manifest["event_count"]:
        raise ValueError(f"{directory}: event count differs from run manifest")
    start = parse_time(json.loads((source / "manifest.json").read_text())["start_time"])
    end = start + timedelta(minutes=json.loads((source / "manifest.json").read_text())["minute_count"])
    contract = load_contract()
    for prediction, evidence_row, audit_row in zip(predictions, evidence, audit):
        validate_prediction(prediction, contract)
        event_id = prediction["prediction_id"]
        if evidence_row["event_id"] != event_id or audit_row["event_id"] != event_id:
            raise ValueError(f"{directory}: prediction, evidence and audit event ids differ")
        if (prediction["start_time"] != evidence_row["start_time"]
                or prediction["end_time"] != evidence_row["end_time"]):
            raise ValueError(f"{directory}: prediction and evidence times differ: {event_id}")
        if not start <= parse_time(prediction["start_time"]) < parse_time(prediction["end_time"]) <= end:
            raise ValueError(f"{directory}: prediction outside input batch interval: {event_id}")
        if _pack(evidence_row).sha256() != evidence_row["evidence_sha256"]:
            raise ValueError(f"{directory}: evidence hash mismatch: {event_id}")
        candidate_nodes = {item["node"] for item in evidence_row["candidates"]}
        if any(item["network_element_id"] not in candidate_nodes
               for item in prediction["root_cause_top5"]):
            raise ValueError(f"{directory}: predicted root outside evidence candidates: {event_id}")
    if len({item["prediction_id"] for item in predictions}) != len(predictions):
        raise ValueError(f"{directory}: duplicate prediction ids")
    categories = Counter("/".join((item["fault_category"]["major_category"],
                                   item["fault_category"]["sub_category"]))
                         for item in predictions)
    tiers = Counter(item["evidence_tier"] for item in audit)
    return {
        "run_dir": str(directory),
        "input_store": str(source),
        "source_profile": manifest.get("source_profile", "unknown"),
        "prediction_count": len(predictions),
        "categories": dict(sorted(categories.items())),
        "evidence_tiers": dict(sorted(tiers.items())),
        "root_has_direct_evidence": sum(bool(item["root_has_direct_evidence"]) for item in audit),
        "traffic_observation_count": sum(len(item.get("traffic_observations", ())) for item in evidence),
        "input_start": start.isoformat(),
        "input_end": end.isoformat(),
    }, predictions


def audit_two_batches(stage1_run: Path, stage2_run: Path, merged_path: Path) -> dict:
    """Fail on inconsistency; report distribution and overlap without claiming accuracy."""
    first, first_rows = _audit_run(stage1_run)
    second, second_rows = _audit_run(stage2_run)
    if parse_time(first["input_end"]) > parse_time(second["input_start"]):
        raise ValueError("batch input intervals overlap or are in the wrong order")
    merged_path = Path(merged_path)
    with TemporaryDirectory() as temporary:
        replay = Path(temporary) / "merged.jsonl"
        merge_predictions([Path(stage1_run) / "predictions.jsonl",
                           Path(stage2_run) / "predictions.jsonl"], replay)
        if replay.read_bytes() != merged_path.read_bytes():
            raise ValueError("merged file differs from exact replay of the two batch runs")
    overlap = _overlapping_same_category(first_rows) + _overlapping_same_category(second_rows)
    return {
        "verified": True,
        "prediction_count": len(first_rows) + len(second_rows),
        "merged_file": str(merged_path),
        "merged_sha256": _sha256(merged_path),
        "batches": [first, second],
        "same_category_overlap_pairs": len(overlap),
        "overlap_examples": overlap[:20],
        "accuracy_caveat": "No hidden ground truth was used; this is a consistency audit, not a score estimate.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit two v3 run directories and their merged JSONL")
    parser.add_argument("--stage1-run", type=Path, required=True)
    parser.add_argument("--stage2-run", type=Path, required=True)
    parser.add_argument("--merged", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = audit_two_batches(args.stage1_run, args.stage2_run, args.merged)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in
                      ("verified", "prediction_count", "same_category_overlap_pairs")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Create diagnosis field ablations from two aligned runs, without model calls."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .contracts import load_contract, parse_time, validate_prediction
from .merge import merge_predictions


def _read(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    indexed = {r["prediction_id"]: r for r in rows}
    if len(rows) != len(indexed):
        raise ValueError(f"duplicate prediction ids: {path}")
    for row in rows:
        validate_prediction(row, load_contract())
    return rows, indexed


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_ablations(baseline_root: Path, reviewed_root: Path, output_dir: Path):
    baseline_root, reviewed_root, output_dir = map(Path, (baseline_root, reviewed_root, output_dir))
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output_dir}")
    variants = {name: {} for name in ("rules", "both", "roots_only", "category_only")}
    report = {"batches": {}, "input_sha256": {},
              "accuracy_caveat": "Field ablations are not ground-truth evaluations; submit separately to compare scores."}
    for stage in ("stage1", "stage2"):
        base_file = baseline_root / stage / "predictions.jsonl"
        llm_file = reviewed_root / stage / "predictions.jsonl"
        rows, base = _read(base_file)
        _, reviewed = _read(llm_file)
        if base.keys() != reviewed.keys():
            raise ValueError(f"{stage}: event ids differ; detection must remain fixed for diagnosis ablation")
        manifests = [root / stage / "run_manifest.json" for root in (baseline_root, reviewed_root)]
        if any(p.exists() for p in manifests):
            if not all(p.exists() for p in manifests):
                raise ValueError(f"{stage}: missing run manifest")
            first, second = [json.loads(p.read_text(encoding="utf-8")) for p in manifests]
            for key in ("input_manifest_sha256", "input_array_sha256", "input_sidecar_sha256", "detector_settings"):
                if first.get(key) != second.get(key):
                    raise ValueError(f"{stage}: different {key}")
            for path, manifest in zip((base_file, llm_file), (first, second)):
                if _sha(path) != manifest["output_sha256"]["predictions.jsonl"]:
                    raise ValueError(f"modified predictions: {path}")
        counts = Counter()
        examples = []
        for row in rows:
            other = reviewed[row["prediction_id"]]
            if any(parse_time(row[k]) != parse_time(other[k]) for k in ("start_time", "end_time")):
                raise ValueError(f"{stage}: time boundary differs: {row['prediction_id']}")
            a = [r["network_element_id"] for r in row["root_cause_top5"]]
            b = [r["network_element_id"] for r in other["root_cause_top5"]]
            counts["top5_changed"] += a != b
            counts["top1_changed"] += a[0] != b[0]
            counts["top5_membership_changed"] += set(a) != set(b)
            counts["category_changed"] += row["fault_category"] != other["fault_category"]
            if (a != b or row["fault_category"] != other["fault_category"]) and len(examples) < 20:
                examples.append({"event_id": row["prediction_id"], "rules_roots": a, "reviewed_roots": b,
                                 "rules_category": row["fault_category"], "reviewed_category": other["fault_category"]})
            variants["rules"].setdefault(stage, []).append(row)
            variants["both"].setdefault(stage, []).append(other)
            variants["roots_only"].setdefault(stage, []).append({**row, "root_cause_top5": other["root_cause_top5"]})
            variants["category_only"].setdefault(stage, []).append({**row, "fault_category": other["fault_category"]})
        # Keep empty batches representable too.
        for stages in variants.values():
            stages.setdefault(stage, [])
        report["batches"][stage] = {"event_count": len(rows), **dict(counts), "examples": examples}
        for path in (base_file, llm_file):
            report["input_sha256"][str(path)] = _sha(path)
    # Validate all variants before publishing any output; merge also checks duplicate events.
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        for name, stages in variants.items():
            paths = []
            for stage, rows in stages.items():
                path = directory / f"{name}_{stage}.jsonl"
                path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
                paths.append(path)
            merge_predictions(paths, directory / f"{name}.jsonl")
        report["output_sha256"] = {f"{name}.jsonl": _sha(directory / f"{name}.jsonl") for name in variants}
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in variants:
            (output_dir / f"{name}.jsonl").write_bytes((directory / f"{name}.jsonl").read_bytes())
        (output_dir / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description="Compare and create aligned diagnosis ablations")
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--reviewed-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = create_ablations(args.baseline_root, args.reviewed_root, args.output_dir)
    print(json.dumps({"batches": {k: {n: v for n, v in row.items() if n != "examples"}
                                  for k, row in report["batches"].items()},
                      "output_dir": str(args.output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

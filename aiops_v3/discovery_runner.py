"""Sharded LLM discovery runner: clusters -> model -> validated predictions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor

from .contracts import load_contract
from .detection import DetectorSettings
from .discovery import build_clusters, build_cluster_predictions, build_request, _prompt_sha256
from .server_llm import _post_json


def _read_jsonl(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _settings(netflow_stats: Path | None, *, keep_weak_cpu: bool,
              routing_change: bool, subset_minutes: int,
              prefix_aware: bool) -> DetectorSettings:
    return DetectorSettings(
        weak_cpu_max_minutes=3,
        weak_cpu_max_score=8,
        routing_dimension_detection=True,
        netflow_events=netflow_stats is not None,
        netflow_stats_path=str(netflow_stats) if netflow_stats else None,
        netflow_subset_minutes=subset_minutes,
        routing_change_detection=routing_change,
        prefix_aware_blackhole=prefix_aware,
        keep_weak_cpu=keep_weak_cpu,
    )


def _call(complete, payload: dict) -> dict:
    raw = complete(payload)
    body = json.loads(raw["choices"][0]["message"]["content"])
    if not isinstance(body, dict):
        raise ValueError("discovery output is not a JSON object")
    return body


def run_shard(*, input_store: Path, netflow_stats: Path | None, out_dir: Path,
              model: str, revision: str, base_url: str, api_key: str | None,
              shard: int, shards: int, limit: int | None, timeout: int,
              keep_weak_cpu: bool, routing_change: bool, subset_minutes: int,
              prefix_aware: bool, complete=None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    contract = load_contract()
    settings = _settings(netflow_stats, keep_weak_cpu=keep_weak_cpu,
                         routing_change=routing_change, subset_minutes=subset_minutes,
                         prefix_aware=prefix_aware)
    store, bundles = build_clusters(input_store, netflow_stats, detector_settings=settings)
    cluster_ids = [cid for index, cid in enumerate(sorted(bundles)) if index % shards == shard]
    if limit is not None:
        cluster_ids = cluster_ids[:limit]
    cache_path = out_dir / f"discovery_shard{shard}.jsonl"
    audit_path = out_dir / f"audit_shard{shard}.jsonl"
    pred_path = out_dir / f"predictions_shard{shard}.jsonl"
    model_id = {"repository": model, "revision": revision}
    cached: dict[str, dict] = {}
    for row in _read_jsonl(cache_path):
        if row.get("prompt_sha256") and row.get("model") == model_id:
            cached[row["cluster_id"]] = row
    complete = complete or _post_json(base_url, api_key, timeout)
    summary = {"shard": shard, "shards": shards, "clusters": len(cluster_ids),
               "cached": len(cached), "llm": 0, "fallback": 0, "predictions": 0}
    predictions: list[dict] = []
    with cache_path.open("a", encoding="utf-8") as cache_file, \
            audit_path.open("a", encoding="utf-8") as audit_file:
        for cluster_id in cluster_ids:
            bundle = bundles[cluster_id]
            payload = build_request(bundle, contract)
            prompt_sha = _prompt_sha256(payload)
            material_sha = hashlib.sha256(json.dumps(
                bundle.material, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            row = cached.get(cluster_id)
            if (row is None or row.get("prompt_sha256") != prompt_sha
                    or row.get("material_sha256") != material_sha):
                started = time.monotonic()
                body, error = None, None
                for _attempt in range(2):
                    try:
                        body = _call(complete, payload)
                        error = None
                        break
                    except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
                        error = f"{type(exc).__name__}"
                row = {"cluster_id": cluster_id, "prompt_sha256": prompt_sha,
                       "material_sha256": material_sha,
                       "model": model_id, "body": body, "error": error,
                       "seconds": round(time.monotonic() - started, 3)}
                cache_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                cache_file.flush()
                summary["llm"] += 1
            records, audit = build_cluster_predictions(store, bundle, row.get("body"), contract)
            audit.update({"cluster_id": cluster_id, "llm_error": row.get("error"),
                          "cached": cluster_id in cached})
            audit_file.write(json.dumps(audit, ensure_ascii=False) + "\n")
            audit_file.flush()
            if audit["status"] == "llm_discovery":
                summary["llm"] += (0)
            else:
                summary["fallback"] += 1
            predictions.extend(records)
    predictions.sort(key=lambda item: (item["start_time"], item["end_time"]))
    with pred_path.open("w", encoding="utf-8") as handle:
        for record in predictions:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    summary["predictions"] = len(predictions)
    (out_dir / f"summary_shard{shard}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def combine(out_dir: Path, output: Path) -> dict:
    from .contracts import validate_prediction

    contract = load_contract()
    rows: list[dict] = []
    for path in sorted(Path(out_dir).glob("predictions_shard*.jsonl")):
        rows.extend(_read_jsonl(path))
    rows.sort(key=lambda item: (item["start_time"], item["end_time"]))
    seen = set()
    for index, row in enumerate(rows, 1):
        row["prediction_id"] = f"pred_{index:06d}"
        validate_prediction(row, contract)
        key = (row["start_time"], row["end_time"],
               tuple(root["network_element_id"] for root in row["root_cause_top5"]),
               row["fault_category"]["major_category"], row["fault_category"]["sub_category"])
        if key in seen:
            raise ValueError(f"duplicate discovery prediction at {row['prediction_id']}")
        seen.add(key)
    Path(output).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    return {"rows": len(rows), "output": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-first incident discovery")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input-store", type=Path, required=True)
    run.add_argument("--netflow-stats", type=Path)
    run.add_argument("--out-dir", type=Path, required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--revision", required=True)
    run.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    run.add_argument("--shard", type=int, default=0)
    run.add_argument("--shards", type=int, default=1)
    run.add_argument("--limit", type=int)
    run.add_argument("--timeout", type=int, default=180)
    run.add_argument("--keep-weak-cpu", action="store_true")
    run.add_argument("--routing-change", action="store_true")
    run.add_argument("--subset-minutes", type=int, default=3)
    run.add_argument("--prefix-aware-blackhole", action="store_true")
    combine_parser = sub.add_parser("combine")
    combine_parser.add_argument("--out-dir", type=Path, required=True)
    combine_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "combine":
        print(json.dumps(combine(args.out_dir, args.output), ensure_ascii=False))
        return
    summary = run_shard(
        input_store=args.input_store, netflow_stats=args.netflow_stats,
        out_dir=args.out_dir, model=args.model, revision=args.revision,
        base_url=args.base_url, api_key=os.environ.get("VLLM_API_KEY"),
        shard=args.shard, shards=args.shards, limit=args.limit,
        timeout=args.timeout, keep_weak_cpu=args.keep_weak_cpu,
        routing_change=args.routing_change, subset_minutes=args.subset_minutes,
        prefix_aware=args.prefix_aware_blackhole,
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

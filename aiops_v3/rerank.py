"""Cross-device root-cause re-ranking with objective, evidence-bound gating.

The reranker takes an existing prediction file (windows and categories stay
fixed) and rebuilds, for every event, the *full* per-device observation table
inside the event window - not only the signals the detector happened to attach.
The model compares devices and ranks roots (BiAn-style); an objective gate only
accepts a different rank 1 when the new device has better support (direct
evidence, stronger peak, or earlier onset).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .contracts import OfficialContract, load_contract, parse_time
from .detection import _NODE_RULES, _Rule, _scores
from .server_llm import _post_json

SHIFT = 2  # minutes around the event window used to collect observations
MAX_CANDIDATES = 12
MAX_EVIDENCE_PER_NODE = 6
_SCHEMA_VERSION = "v3-rerank-1"


def _store_for(day: str):
    if day < "2026-09-10":
        return "stage1"
    if day < "2026-10-01":
        return "stage2"
    return "stage3"


def _node_role(node: str) -> str:
    parts = node.split("-")
    return parts[1] if len(parts) > 1 else ""


class BatchIndex:
    """Per-store rule score matrices, computed once."""

    def __init__(self, store):
        self.store = store
        self.start = parse_time(store.manifest["start_time"])
        self.count = store.manifest["minute_count"]
        self.rules = list(_NODE_RULES) + [
            _Rule("routing.ipv6_route_change_total", "bgp_route_flap", 1, 0.5, 2.0)]
        self.matrices: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for rule in self.rules:
            column = store.feature_index("node", rule.feature)
            if column is None:
                continue
            data = np.asarray(store.node_values[:, :, column], np.float32)
            mask = np.asarray(store.node_mask[:, :, column], bool)
            score, center = _scores(data, mask, rule)
            self.matrices[rule.feature] = (score, center)
        self.nodes = list(store.nodes)

    def window_signals(self, start: int, end: int) -> dict[str, list[dict]]:
        lo, hi = max(0, start - SHIFT), min(self.count, end + SHIFT)
        observed: dict[str, list[dict]] = {}
        for rule in self.rules:
            pair = self.matrices.get(rule.feature)
            if pair is None:
                continue
            score, center = pair
            block = score[lo:hi]
            if not block.size:
                continue
            for node_index, node in enumerate(self.nodes):
                column = block[:, node_index]
                if column.size == 0:
                    continue
                position = int(np.argmax(column))
                peak = float(column[position])
                if peak < 4.0:
                    continue
                observed.setdefault(node, []).append({
                    "evidence_id": f"node:{lo + position}:{node}:{rule.feature}",
                    "feature": rule.feature,
                    "category": rule.category,
                    "minute": lo + position,
                    "offset": (lo + position) - start,
                    "value": round(float(self.store.node_values[lo + position, node_index,
                                                                self.store.feature_index('node', rule.feature)]), 6),
                    "reference": round(float(center[node_index]), 6),
                    "score": round(peak, 4),
                })
        for node in observed:
            observed[node].sort(key=lambda item: (-item["score"], item["minute"]))
            observed[node] = observed[node][:MAX_EVIDENCE_PER_NODE]
        return observed


def _observers(stats_rows, start: int, end: int) -> list[dict]:
    cells: dict[tuple[str, str], list[float]] = {}
    for row in stats_rows:
        minute = int(row["minute"])
        if not start - SHIFT <= minute < end + SHIFT:
            continue
        key = (row["service"], row["observer_city"])
        cell = cells.setdefault(key, [0.0, 0.0])
        cell[0] += float(row.get("req_pkts") or 0)
        cell[1] += float(row.get("resp_pkts") or 0)
    out = []
    for (service, city), (req, resp) in cells.items():
        ratio = resp / req if req > 0 else 0.0
        out.append({"service": service, "source_city": city,
                    "req": round(req, 1), "resp": round(resp, 1),
                    "ratio": round(ratio, 4),
                    "low": bool(req >= 100 and ratio < 0.5)})
    out.sort(key=lambda item: (item["ratio"], item["service"], item["source_city"]))
    return out[:24]


def _material(event: dict, index: BatchIndex, signals: dict[str, list[dict]],
              observers: list[dict], contract: OfficialContract) -> dict:
    start = int((parse_time(event["start_time"]) - index.start).total_seconds() // 60)
    end = int((parse_time(event["end_time"]) - index.start).total_seconds() // 60)
    current_roots = [item["network_element_id"] for item in event["root_cause_top5"]]
    city = current_roots[0].split("-")[0]
    candidates: list[str] = []
    for node in sorted(signals, key=lambda item: -signals[item][0]["score"]):
        if node in contract.nodes:
            candidates.append(node)
    for node in current_roots:
        if node not in candidates:
            candidates.append(node)
    for observer in observers:
        if observer["low"]:
            for role in ("br-1", "fw", "cr-1"):
                node = f"{observer['source_city']}-{role}"
                if node in contract.nodes and node not in candidates:
                    candidates.append(node)
    for role in ("fw", "br-1", "br-2", "cr-1", "cr-2",
                 "service-vm-1", "service-vm-2", "service-vm-3"):
        node = f"{city}-{role}"
        if node in contract.nodes and node not in candidates:
            candidates.append(node)
    candidates = candidates[:MAX_CANDIDATES]
    signal_index: dict[str, str] = {}
    table = []
    for node in candidates:
        evidence = signals.get(node, [])
        for item in evidence:
            signal_index[item["evidence_id"]] = node
        table.append({
            "network_element_id": node,
            "city": node.split("-")[0],
            "role": _node_role(node),
            "observations": evidence,
        })
    material = {
        "event": {
            "event_id": event["prediction_id"],
            "start_time": event["start_time"], "end_time": event["end_time"],
            "current_root_cause_top5": current_roots,
            "current_fault_category": event["fault_category"],
        },
        "device_observations": table,
        "netflow_observers": observers,
        "topology": {node: [f"city:{node.split('-')[0]}", f"role:{_node_role(node)}"]
                     for node in candidates},
        "instruction": (
            "Rank the most likely root-cause network elements for this one event. "
            "Compare devices: prefer the device whose own observations show a direct "
            "fault mechanism and whose onset is early; a device that only shows a "
            "symptom or is merely in the same city is weaker. The current ranking "
            "is a rule baseline, not truth. Cite each device's own evidence ids "
            "(from its observations); rank 1 must cite evidence or, only for a "
            "service target with at least two low observers, rely on that observer "
            "table. Never invent ids. Return JSON only."
        ),
    }
    return {**material, "_signal_index": signal_index, "_start": start, "_end": end,
            "_candidates": candidates}


def _request(material: dict) -> dict:
    clean = {key: value for key, value in material.items() if not key.startswith("_")}
    schema = {
        "type": "object",
        "properties": {
            "ranked": {
                "type": "array", "minItems": 1, "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "rank": {"type": "integer"},
                        "network_element_id": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                    "required": ["rank", "network_element_id", "evidence_ids", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["ranked"],
        "additionalProperties": False,
    }
    return {
        "messages": [
            {"role": "system", "content": clean["instruction"]},
            {"role": "user", "content": json.dumps(clean, ensure_ascii=False, allow_nan=False)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "v3_rerank", "schema": schema}},
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature": 0,
        "max_tokens": 600,
    }


def _prompt_sha(payload: dict, material: dict) -> str:
    fixed = {"system": payload["messages"][0]["content"],
             "schema": payload["response_format"],
             "decoding": [payload["temperature"], payload["max_tokens"]],
             "user": payload["messages"][1]["content"]}
    return hashlib.sha256(json.dumps(fixed, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_rerank(material: dict, body: dict) -> str | None:
    if not isinstance(body, dict) or set(body) != {"ranked"}:
        return "invalid_schema"
    ranked = body["ranked"]
    if not isinstance(ranked, list) or not 1 <= len(ranked) <= 5:
        return "rank_invalid"
    candidates = set(material["_candidates"])
    signals = material["_signal_index"]
    seen = set()
    for position, item in enumerate(ranked, 1):
        if not isinstance(item, dict) or set(item) != {"rank", "network_element_id",
                                                       "evidence_ids", "reason"}:
            return "invalid_schema"
        if item["rank"] != position:
            return "rank_invalid"
        node = item["network_element_id"]
        if node not in candidates or node in seen:
            return "rank_invalid"
        seen.add(node)
        for evidence_id in item["evidence_ids"]:
            if evidence_id not in signals:
                return "unknown_evidence"
            if signals[evidence_id] != node:
                return "cross_device_evidence"
    first = ranked[0]
    if first["evidence_ids"]:
        return None
    if any(row["low"] for row in material.get("netflow_observers", [])):
        return None
    return "weak_rank1_evidence"


def _observation(material: dict, node: str) -> list[dict]:
    for row in material.get("device_observations", []):
        if row["network_element_id"] == node:
            return row["observations"]
    return []


def _peak(material: dict, node: str) -> float:
    observations = _observation(material, node)
    return max((item["score"] for item in observations), default=0.0)


def _onset(material: dict, node: str) -> int | None:
    observations = _observation(material, node)
    return min((item["minute"] for item in observations), default=None)


def apply_rerank(event: dict, material: dict, body: dict | None) -> tuple[dict, str]:
    row = json.loads(json.dumps(event))
    if body is None:
        return row, "fallback_no_response"
    reason = validate_rerank(material, body)
    if reason is not None:
        return row, f"fallback_invalid:{reason}"
    new_roots = [item["network_element_id"] for item in body["ranked"]]
    current = [item["network_element_id"] for item in row["root_cause_top5"]]
    new_first, current_first = new_roots[0], current[0]
    if new_first == current_first:
        return row, "kept_baseline_rank1"
    new_peak, current_peak = _peak(material, new_first), _peak(material, current_first)
    new_onset, current_onset = _onset(material, new_first), _onset(material, current_first)
    direct_new = new_peak > 0
    direct_current = current_peak > 0
    better = (
        (direct_new and not direct_current)
        or (new_peak >= current_peak + 1.5)
        or (direct_new and direct_current and new_onset is not None
            and current_onset is not None and new_onset <= current_onset - 2)
    )
    if not better:
        return row, "kept_baseline_gate"
    padded = list(new_roots)
    for node in current:
        if len(padded) >= 5:
            break
        if node not in padded:
            padded.append(node)
    row["root_cause_top5"] = [{"rank": index, "network_element_id": node}
                              for index, node in enumerate(padded[:5], 1)]
    return row, "reranked"


def build_rows(predictions: list[dict], stores: dict[str, object],
               stats: dict[str, list[dict]], contract: OfficialContract) -> list[dict]:
    indices = {stage: BatchIndex(store) for stage, store in stores.items()}
    items = []
    for event in predictions:
        stage = _store_for(event["start_time"][:10])
        index = indices[stage]
        start = int((parse_time(event["start_time"]) - index.start).total_seconds() // 60)
        end = int((parse_time(event["end_time"]) - index.start).total_seconds() // 60)
        signals = index.window_signals(start, end)
        observers = _observers(stats.get(stage, []), start, end)
        material = _material(event, index, signals, observers, contract)
        payload = _request(material)
        items.append({"event": event, "material": material, "payload": payload,
                      "prompt_sha256": _prompt_sha(payload, material)})
    return items


def run_rerank(*, predictions: Path, out_dir: Path, model: str, revision: str,
               base_url: str, api_key: str | None, shard: int, shards: int,
               limit: int | None, timeout: int, complete=None) -> dict:
    from .store import open_store

    contract = load_contract()
    roots = Path("data/feature_store/v3")
    stores = {
        "stage1": open_store(roots / "stage1_canonical_20260930"),
        "stage2": open_store(roots / "stage2_canonical_20261001"),
        "stage3": open_store(roots / "stage3_canonical_20261017"),
    }
    stats = {}
    for stage, name in (("stage1", "stage1_service.jsonl"),
                        ("stage2", "stage2_service.jsonl"),
                        ("stage3", "stage3_service.jsonl")):
        path = Path("data/netflow_stats") / name
        stats[stage] = [json.loads(line) for line in path.read_text().splitlines()
                        if line.strip()] if path.exists() else []
    events = [json.loads(line) for line in Path(predictions).read_text().splitlines()
              if line.strip()]
    items = build_rows(events, stores, stats, contract)
    items = [item for index, item in enumerate(items) if index % shards == shard]
    if limit is not None:
        items = items[:limit]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / f"rerank_shard{shard}.jsonl"
    pred_path = out_dir / f"predictions_shard{shard}.jsonl"
    model_id = {"repository": model, "revision": revision}
    cached: dict[str, dict] = {}
    if cache_path.exists():
        for row in (json.loads(line) for line in cache_path.read_text().splitlines() if line.strip()):
            cached[row["event_id"]] = row
    complete = complete or _post_json(base_url, api_key, timeout)
    summary = {"shard": shard, "shards": shards, "events": len(items),
               "llm": 0, "cached": 0, "reranked": 0, "kept": 0, "fallback": 0}
    predictions_out = []
    with cache_path.open("a", encoding="utf-8") as cache_file:
        for item in items:
            event = item["event"]
            event_id = event["prediction_id"]
            row = cached.get(event_id)
            if row is None or row.get("prompt_sha256") != item["prompt_sha256"]:
                started = time.monotonic()
                body, error = None, None
                for _attempt in range(2):
                    try:
                        raw = complete(item["payload"])
                        body = json.loads(raw["choices"][0]["message"]["content"])
                        error = None
                        break
                    except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
                        error = type(exc).__name__
                row = {"event_id": event_id, "prompt_sha256": item["prompt_sha256"],
                       "material_sha256": hashlib.sha256(json.dumps(
                           {k: v for k, v in item["material"].items() if not k.startswith("_")},
                           ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                       "model": model_id, "body": body, "error": error,
                       "seconds": round(time.monotonic() - started, 3)}
                cache_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                cache_file.flush()
                summary["llm"] += 1
            else:
                summary["cached"] += 1
            updated, status = apply_rerank(event, item["material"], row.get("body"))
            if status.startswith("reranked"):
                summary["reranked"] += 1
            elif status.startswith("kept"):
                summary["kept"] += 1
            else:
                summary["fallback"] += 1
            predictions_out.append(updated)
    pred_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                 for row in predictions_out))
    (out_dir / f"summary_shard{shard}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def combine(out_dir: Path, output: Path) -> dict:
    from .contracts import validate_prediction

    contract = load_contract()
    rows = []
    for path in sorted(Path(out_dir).glob("predictions_shard*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    rows.sort(key=lambda item: (item["start_time"], item["end_time"]))
    for index, row in enumerate(rows, 1):
        row["prediction_id"] = f"pred_{index:06d}"
        validate_prediction(row, contract)
    Path(output).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    return {"rows": len(rows), "output": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-device root-cause re-ranking")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--predictions", type=Path, required=True)
    run.add_argument("--out-dir", type=Path, required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--revision", required=True)
    run.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    run.add_argument("--shard", type=int, default=0)
    run.add_argument("--shards", type=int, default=1)
    run.add_argument("--limit", type=int)
    run.add_argument("--timeout", type=int, default=180)
    combine_parser = sub.add_parser("combine")
    combine_parser.add_argument("--out-dir", type=Path, required=True)
    combine_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "combine":
        print(json.dumps(combine(args.out_dir, args.output), ensure_ascii=False))
        return
    summary = run_rerank(predictions=args.predictions, out_dir=args.out_dir,
                         model=args.model, revision=args.revision, base_url=args.base_url,
                         api_key=os.environ.get("VLLM_API_KEY"), shard=args.shard,
                         shards=args.shards, limit=args.limit, timeout=args.timeout)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

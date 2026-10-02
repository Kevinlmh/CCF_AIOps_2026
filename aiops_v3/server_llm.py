"""Batch evidence-only diagnosis against a local vLLM chat endpoint."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import time
from urllib import request

from .contracts import load_contract
from .detection import Signal
from .diagnosis import Candidate, EvidencePack, diagnose_rules
from .optional_llm import choose_diagnosis


def _pack(record: dict) -> EvidencePack:
    return EvidencePack(
        record["event_id"], record["start_time"], record["end_time"],
        tuple(Signal(**row) for row in record["signals"]),
        tuple(Candidate(row["node"], row["score"], tuple(row["evidence_ids"]), row["reason"])
              for row in record["candidates"]),
        tuple(record.get("temporal_context", ())),
    )


def _request_payload(pack: EvidencePack, model: str, contract) -> dict:
    candidates = [item.node for item in pack.candidates]
    evidence_ids = [item.evidence_id for item in pack.signals]
    category_pairs = sorted(contract.categories)
    rules = diagnose_rules(pack, contract)
    schema = {
        "type": "object",
        "properties": {
            "root_cause_top5": {
                "type": "array", "items": {"type": "string", "enum": candidates},
                "minItems": 5, "maxItems": 5, "uniqueItems": True,
            },
            "fault_category": {
                "type": "object",
                "properties": {
                    "major_category": {"type": "string", "enum": sorted({pair[0] for pair in category_pairs})},
                    "sub_category": {"type": "string", "enum": sorted({pair[1] for pair in category_pairs})},
                },
                "required": ["major_category", "sub_category"],
                "additionalProperties": False,
            },
            "evidence_ids": {
                "type": "array", "items": {"type": "string", "enum": evidence_ids},
                "minItems": 1, "uniqueItems": True,
            },
        },
        "required": ["root_cause_top5", "fault_category", "evidence_ids"],
        "additionalProperties": False,
    }
    material = {
        "event_id": pack.event_id, "start_time": pack.start_time, "end_time": pack.end_time,
        "signals": [asdict(item) for item in pack.signals],
        "candidates": [asdict(item) for item in pack.candidates],
        "temporal_context": pack.temporal_context,
        "allowed_categories": category_pairs,
        "rule_baseline": {"root_cause_top5": rules.roots, "fault_category": rules.category},
    }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "Diagnose one network incident from observed evidence only. Rank five distinct nodes "
                "from candidates and choose one allowed major/sub category pair. Use temporal order and "
                "direct evidence before auxiliary context. Unknown mappings are not proof. If evidence "
                "cannot justify a change, keep the rule baseline. Cite existing evidence_ids. Return JSON only."
            )},
            {"role": "user", "content": json.dumps(material, ensure_ascii=False, allow_nan=False)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "aiops_diagnosis", "schema": schema,
        }},
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature": 0,
        "max_tokens": 512,
    }


def _prompt_sha256(payload: dict) -> str:
    """Fingerprint instruction, schema, and decoding independently of event data."""
    fixed = {
        "system": payload["messages"][0]["content"],
        "response_format": payload["response_format"],
        "chat_template_kwargs": payload["chat_template_kwargs"],
        "temperature": payload["temperature"],
        "max_tokens": payload["max_tokens"],
    }
    encoded = json.dumps(fixed, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def diagnose_records(records: list[dict], complete, model: str,
                     revision: str) -> tuple[list[dict], list[dict]]:
    """Return only responses accepted by the existing strict importer."""
    contract = load_contract()
    accepted = []
    audit = []
    for record in records:
        event_id = record.get("event_id")
        started = time.monotonic()
        try:
            pack = _pack(record)
            if pack.sha256() != record["evidence_sha256"]:
                raise ValueError("evidence hash mismatch")
            payload = _request_payload(pack, model, contract)
            raw = complete(payload)
            body = json.loads(raw["choices"][0]["message"]["content"])
            if not isinstance(body, dict):
                audit.append({"event_id": event_id, "status": "invalid_llm_response",
                              "seconds": round(time.monotonic() - started, 3)})
                continue
            response = {
                "event_id": pack.event_id, "evidence_sha256": record["evidence_sha256"],
                "root_cause_top5": body.get("root_cause_top5"),
                "fault_category": body.get("fault_category"),
                "evidence_ids": body.get("evidence_ids"),
                "model": {"repository": model, "revision": revision},
                "prompt_sha256": _prompt_sha256(payload),
            }
            _, reason = choose_diagnosis(pack, diagnose_rules(pack, contract), response, contract)
            if reason is not None:
                audit.append({"event_id": event_id, "status": reason,
                              "seconds": round(time.monotonic() - started, 3)})
                continue
            accepted.append(response)
            audit.append({"event_id": event_id, "status": "accepted",
                          "seconds": round(time.monotonic() - started, 3)})
        except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
            audit.append({"event_id": event_id, "status": "request_failed",
                          "error": type(exc).__name__,
                          "seconds": round(time.monotonic() - started, 3)})
    return accepted, audit


def _post_json(endpoint: str, api_key: str | None, timeout: int):
    def complete(payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        item = request.Request(endpoint.rstrip("/") + "/chat/completions",
                               data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                               headers=headers, method="POST")
        with request.urlopen(item, timeout=timeout) as response:
            return json.load(response)
    return complete


def run_batch(evidence_path: Path, output: Path, audit_path: Path, *, model: str,
              revision: str, base_url: str, api_key: str | None = None,
              limit: int | None = None, timeout: int = 120) -> dict:
    rows = [json.loads(line) for line in Path(evidence_path).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if limit is not None:
        rows = rows[:limit]
    if len({row["event_id"] for row in rows}) != len(rows):
        raise ValueError("evidence file has duplicate event ids; process each batch separately")
    output, audit_path = Path(output), Path(audit_path)
    existing = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
                if line.strip()] if output.exists() else []
    by_id = {row["event_id"]: row for row in existing}
    if len(by_id) != len(existing):
        raise ValueError("existing response file has duplicate event ids")
    contract = load_contract()
    for row in rows:
        response = by_id.get(row["event_id"])
        if response and (response.get("evidence_sha256") != row["evidence_sha256"]
                         or response.get("model") != {"repository": model, "revision": revision}):
            raise ValueError("existing response does not match evidence or model revision")
        if response:
            pack = _pack(row)
            if pack.sha256() != row["evidence_sha256"]:
                raise ValueError("existing evidence hash mismatch")
            if response.get("prompt_sha256") != _prompt_sha256(_request_payload(pack, model, contract)):
                raise ValueError("existing response does not match prompt or decoding settings")
            _, reason = choose_diagnosis(pack, diagnose_rules(pack, contract), response, contract)
            if reason is not None:
                raise ValueError(f"invalid cached response for {row['event_id']}: {reason}")
    pending = [row for row in rows if row["event_id"] not in by_id]
    output.parent.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    complete = _post_json(base_url, api_key, timeout)
    accepted_count = 0
    with output.open("a", encoding="utf-8") as responses, audit_path.open("a", encoding="utf-8") as audits:
        for row in pending:
            accepted, audit = diagnose_records([row], complete, model, revision)
            for item in accepted:
                responses.write(json.dumps(item, ensure_ascii=False) + "\n")
                responses.flush()
                accepted_count += 1
            audits.write(json.dumps(audit[0], ensure_ascii=False) + "\n")
            audits.flush()
    return {"input_count": len(rows), "already_accepted": len(rows) - len(pending),
            "new_accepted": accepted_count, "new_fallbacks": len(pending) - accepted_count,
            "output": str(output), "audit": str(audit_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate evidence-bound local LLM responses")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    summary = run_batch(args.evidence, args.output, args.audit, model=args.model,
                        revision=args.revision, base_url=args.base_url,
                        api_key=os.environ.get("VLLM_API_KEY"), limit=args.limit,
                        timeout=args.timeout)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

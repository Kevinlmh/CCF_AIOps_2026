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
from urllib.error import HTTPError

from .contracts import load_contract
from .detection import Signal
from .diagnosis import Candidate, EvidencePack, diagnose_rules
from .optional_llm import choose_diagnosis, _validate_diagnosis


def _pack(record: dict) -> EvidencePack:
    return EvidencePack(
        record["event_id"], record["start_time"], record["end_time"],
        tuple(Signal(**row) for row in record["signals"]),
        tuple(Candidate(row["node"], row["score"], tuple(row["evidence_ids"]), row["reason"])
              for row in record["candidates"]),
        tuple(record.get("temporal_context", ())),
        tuple(record.get("traffic_observations", ())),
        tuple(record.get("device_context", ())),
        tuple(record.get("text_evidence", ())),
        tuple(record.get("available_sources", ())),
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
                "minItems": 5, "maxItems": 5,
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
                "minItems": 1, "maxItems": 6,
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
        "traffic_observations": pack.traffic_observations,
        "allowed_categories": category_pairs,
        "rule_baseline": {"root_cause_top5": rules.roots, "fault_category": rules.category},
    }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "Diagnose one network incident from observed evidence only. Rank five distinct nodes "
                "from candidates and choose one allowed major/sub category pair. Use temporal order and "
                "direct evidence before auxiliary context. A traffic observer is a measurement source, "
                "not proof of a faulty root. Unknown mappings are not proof. If evidence "
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
        details = {}
        try:
            pack = _pack(record)
            if pack.sha256() != record["evidence_sha256"]:
                raise ValueError("evidence hash mismatch")
            payload = _request_payload(pack, model, contract)
            raw = complete(payload)
            choice = raw["choices"][0]
            details = {"finish_reason": choice.get("finish_reason"), "usage": raw.get("usage", {})}
            content = choice["message"]["content"]
            details["response_excerpt"] = str(content)[:1500]
            if choice.get("finish_reason") == "length":
                audit.append({"event_id": event_id, "status": "truncated_response", **details,
                              "seconds": round(time.monotonic() - started, 3)})
                continue
            body = json.loads(content)
            if not isinstance(body, dict):
                audit.append({"event_id": event_id, "status": "invalid_llm_response",
                              **details,
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
                              **details,
                              "seconds": round(time.monotonic() - started, 3)})
                continue
            accepted.append(response)
            audit.append({"event_id": event_id, "status": "accepted",
                          "finish_reason": details["finish_reason"], "usage": details["usage"],
                          "seconds": round(time.monotonic() - started, 3)})
        except HTTPError as exc:
            audit.append({"event_id": event_id, "status": "request_failed", "error": "HTTPError",
                          "http_status": exc.code,
                          "detail": exc.read(1500).decode("utf-8", "replace"),
                          "seconds": round(time.monotonic() - started, 3)})
        except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
            audit.append({"event_id": event_id, "status": "request_failed",
                          "error": type(exc).__name__, "detail": str(exc)[:1000], **details,
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
              limit: int | None = None, timeout: int = 120, workflow: str = "single") -> dict:
    if workflow not in {"single", "staged"}:
        raise ValueError(f"unknown LLM workflow: {workflow}")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    from .staged_llm import diagnose_staged, workflow_fingerprint
    rows = [json.loads(line) for line in Path(evidence_path).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if len({row["event_id"] for row in rows}) != len(rows):
        raise ValueError("evidence file has duplicate event ids; process each batch separately")
    source_by_id = {row["event_id"]: row for row in rows}
    if limit is not None:
        rows = rows[:limit]
    if workflow == "staged" and any(not row.get("device_context") for row in rows):
        raise ValueError("staged workflow requires evidence generated with --diagnostic-context")
    output, audit_path = Path(output), Path(audit_path)
    existing = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
                if line.strip()] if output.exists() else []
    by_id = {row["event_id"]: row for row in existing}
    if len(by_id) != len(existing):
        raise ValueError("existing response file has duplicate event ids")
    contract = load_contract()
    # Validate the whole cache even for --limit; outside-subset rows must not
    # silently carry responses from another prompt, workflow, model or input.
    for response in existing:
        row = source_by_id.get(response["event_id"])
        if row is None:
            raise ValueError("existing response event is absent from evidence file")
        if response and (response.get("evidence_sha256") != row["evidence_sha256"]
                         or response.get("model") != {"repository": model, "revision": revision}):
            raise ValueError("existing response does not match evidence or model revision")
        if response:
            pack = _pack(row)
            if pack.sha256() != row["evidence_sha256"]:
                raise ValueError("existing evidence hash mismatch")
            if response.get("workflow", "single") != workflow:
                raise ValueError("existing response does not match workflow")
            expected_prompt = (workflow_fingerprint(pack, model) if workflow == "staged" else
                               _prompt_sha256(_request_payload(pack, model, contract)))
            if response.get("prompt_sha256") != expected_prompt:
                raise ValueError("existing response does not match prompt or decoding settings")
            _, reason = _validate_diagnosis(pack, diagnose_rules(pack, contract), response, contract)
            if reason is not None:
                raise ValueError(f"invalid cached response for {row['event_id']}: {reason}")
    pending = [row for row in rows if row["event_id"] not in by_id]
    output.parent.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    complete = _post_json(base_url, api_key, timeout)
    accepted_count = 0
    with output.open("a", encoding="utf-8") as responses, audit_path.open("a", encoding="utf-8") as audits:
        for row in pending:
            diagnose = diagnose_staged if workflow == "staged" else diagnose_records
            accepted, audit = diagnose([row], complete, model, revision)
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
    parser.add_argument("--workflow", choices=("single", "staged"), default="single")
    args = parser.parse_args()
    summary = run_batch(args.evidence, args.output, args.audit, model=args.model,
                        revision=args.revision, base_url=args.base_url,
                        api_key=os.environ.get("VLLM_API_KEY"), limit=args.limit,
                        timeout=args.timeout, workflow=args.workflow)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

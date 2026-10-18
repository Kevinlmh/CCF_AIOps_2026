"""Resumable four-role runner against an OpenAI-compatible chat endpoint.

The runner never invents a decision: every role response is validated by
:mod:`aiops_v3.roles` before it is accepted, and an invalid response is stored
as ``fallback`` so the importer keeps the rule value for that field.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from urllib import request

from .classification import is_clear_event
from .contracts import load_contract
from .diagnosis import Diagnosis, EvidencePack, diagnose_rules
from .roles import ROLE_NAMES, build_request, validate
from .server_llm import _pack, _post_json


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_sha256(payload: dict) -> str:
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


def _read_jsonl(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _load_evidence(path: Path) -> list[dict]:
    rows = _read_jsonl(path)
    if len({row["event_id"] for row in rows}) != len(rows):
        raise ValueError("evidence file has duplicate event ids")
    return rows


def _resume_index(path: Path, records: list[dict], *, model: str, revision: str,
                  contract) -> dict[tuple[str, str], dict]:
    """Validate cached rows against the current evidence and prompt protocol."""
    cached: dict[tuple[str, str], dict] = {}
    by_event = {row["event_id"]: row for row in records}
    for row in _read_jsonl(path):
        event_id, role = row.get("event_id"), row.get("role")
        if event_id not in by_event or role not in ROLE_NAMES:
            raise ValueError(f"cached row does not match any event/role: {event_id}/{role}")
        record = by_event[event_id]
        if row.get("evidence_sha256") != record["evidence_sha256"]:
            raise ValueError(f"cached row evidence hash mismatch for {event_id}/{role}")
        if row.get("model") != {"repository": model, "revision": revision}:
            raise ValueError(f"cached row model mismatch for {event_id}/{role}")
        pack = _pack(record)
        baseline = diagnose_rules(pack, contract)
        payload = build_request(role, pack, baseline=baseline, upstream={})
        if row.get("prompt_sha256") != _prompt_sha256(payload):
            raise ValueError(f"cached row prompt mismatch for {event_id}/{role}")
        if row.get("status") == "accepted":
            reason = validate(role, pack, row.get("body", {}), baseline=baseline, upstream={})
            if reason is not None and role != "review":
                raise ValueError(f"cached row is no longer valid for {event_id}/{role}: {reason}")
        cached[(event_id, role)] = row
    return cached


def _call(complete, payload: dict) -> dict:
    raw = complete(payload)
    content = raw["choices"][0]["message"]["content"]
    body = json.loads(content)
    if not isinstance(body, dict):
        raise ValueError("role output is not a JSON object")
    return body


def run_roles(evidence_path: Path, out_dir: Path, *, model: str, revision: str,
              base_url: str, api_key: str | None = None, limit: int | None = None,
              timeout: int = 120, scope: str = "all", complete=None,
              retry_fallbacks: bool = False) -> dict:
    if scope not in {"all", "ambiguous"}:
        raise ValueError("unknown role scope")
    contract = load_contract()
    records = _load_evidence(Path(evidence_path))
    if limit is not None:
        records = records[:limit]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    roles_path = out_dir / "roles.jsonl"
    audit_path = out_dir / "role_audit.jsonl"
    cached = _resume_index(roles_path, records, model=model, revision=revision,
                           contract=contract)
    if retry_fallbacks:
        cached = {key: row for key, row in cached.items() if row.get("status") == "accepted"}
    complete = complete or _post_json(base_url, api_key, timeout)
    model_id = {"repository": model, "revision": revision}

    summary = {"events": len(records), "model": model_id, "scope": scope,
               "cached": len(cached), "fastpath": 0,
               "roles": {role: {"accepted": 0, "fallback": 0, "seconds": 0.0}
                         for role in ROLE_NAMES}}
    with roles_path.open("a", encoding="utf-8") as roles_file, \
            audit_path.open("a", encoding="utf-8") as audit_file:
        for record in records:
            pack = _pack(record)
            baseline = diagnose_rules(pack, contract)
            event_id = record["event_id"]
            if scope == "ambiguous" and is_clear_event(pack, baseline):
                summary["fastpath"] += 1
                audit_file.write(json.dumps({"event_id": event_id, "status": "fastpath"},
                                            ensure_ascii=False) + "\n")
                audit_file.flush()
                continue
            upstream: dict[str, dict] = {}
            for role in ROLE_NAMES:
                key = (event_id, role)
                cached_row = cached.get(key)
                if cached_row is not None:
                    if cached_row.get("status") == "accepted":
                        upstream[role] = cached_row.get("body", {})
                    continue
                started = time.monotonic()
                payload = build_request(role, pack, baseline=baseline, upstream=upstream)
                row = {
                    "event_id": event_id,
                    "evidence_sha256": record["evidence_sha256"],
                    "role": role,
                    "prompt_sha256": _prompt_sha256(payload),
                    "model": model_id,
                    "status": "fallback",
                    "body": None,
                    "reason": None,
                }
                try:
                    body = _call(complete, payload)
                    reason = validate(role, pack, body, baseline=baseline, upstream=upstream)
                    if reason is None:
                        row["status"] = "accepted"
                        row["body"] = body
                        upstream[role] = body
                        summary["roles"][role]["accepted"] += 1
                    else:
                        row["reason"] = reason
                        summary["roles"][role]["fallback"] += 1
                except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
                    row["reason"] = f"request_failed:{type(exc).__name__}"
                    summary["roles"][role]["fallback"] += 1
                row["seconds"] = round(time.monotonic() - started, 3)
                summary["roles"][role]["seconds"] += row["seconds"]
                roles_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                roles_file.flush()
                audit_file.write(json.dumps({
                    "event_id": event_id, "role": role, "status": row["status"],
                    "reason": row["reason"], "seconds": row["seconds"],
                }, ensure_ascii=False) + "\n")
                audit_file.flush()
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run four evidence-bound LLM roles")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--role-scope", choices=("all", "ambiguous"), default="all")
    parser.add_argument("--retry-fallbacks", action="store_true")
    args = parser.parse_args()
    summary = run_roles(
        args.evidence, args.out_dir, model=args.model, revision=args.revision,
        base_url=args.base_url, api_key=os.environ.get("VLLM_API_KEY"),
        limit=args.limit, timeout=args.timeout, scope=args.role_scope,
        retry_fallbacks=args.retry_fallbacks,
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

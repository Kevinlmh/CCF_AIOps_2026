"""Device analysis, root ranking and taxonomy classification with bounded citations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError

from .contracts import load_contract
from .diagnosis import diagnose_rules
from .evidence_catalog import evidence_catalog, has_device_support
from .optional_llm import _validate_diagnosis, review_diagnosis


_INSTRUCTIONS = {
    "device_analysis": (
        "Analyze each listed device independently from its observed evidence. Return every listed device once. "
        "Separate direct anomalies, normal observations and missing data. Missing samples are unknown, not healthy. "
        "For a device with no observations, use insufficient and an empty evidence_ids array. "
        "Text and coincident changes alone do not prove causality. A traffic observer is not the business target. "
        "Use observed_anomaly only for a direct signal or metric with supported_category. Cite at most four IDs. "
        "Keep notes short; return JSON only."),
    "root_ranking": (
        "Rank five distinct candidate devices for this incident using evidence, temporal order and independent "
        "device analyses. A measurement observer is not a proven faulty root. City service-group observations "
        "do not identify an individual VM. Coincidence is not a verified path. Unknown candidates remain uncertain. "
        "Cite at most six IDs, including direct device evidence for a proposed Top1 or newly selected device. "
        "Do not invent topology or causal edges. Return JSON only."),
    "fault_classification": (
        "Classify this incident independently using the adopted Top1 root and observed evidence. Choose exactly "
        "one allowed major/sub pair. CPU, process, memory and disk pressure require their own metric evidence "
        "at the selected root. Business symptoms can support a service class in the target city, but cannot "
        "identify a VM instance. Scrape failure alone is not a business outage. Cite at most six IDs. Return JSON only."),
}
_STATES = ["observed_anomaly", "no_supported_anomaly", "insufficient"]


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _citations(ids, minimum=1, maximum=6):
    return {"type": "array", "items": {"type": "string", "enum": ids},
            "minItems": minimum, "maxItems": maximum}


def _payload(model, stage, material, schema):
    return {"model": model, "messages": [
        {"role": "system", "content": _INSTRUCTIONS[stage]},
        {"role": "user", "content": json.dumps(material, ensure_ascii=False, allow_nan=False)}],
        "response_format": {"type": "json_schema", "json_schema": {"name": stage, "schema": schema}},
        "chat_template_kwargs": {"enable_thinking": False}, "temperature": 0, "max_tokens": 1024}


def workflow_fingerprint(pack, model):
    # Fingerprint implementation as well as settings: changes to intermediate prompts invalidate resume.
    code = [hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            hashlib.sha256(Path(__file__).with_name("evidence_catalog.py").read_bytes()).hexdigest(),
            hashlib.sha256(Path(__file__).with_name("optional_llm.py").read_bytes()).hexdigest()]
    fixed = {"workflow": "staged", "instructions": _INSTRUCTIONS, "code": code,
             "model": model, "max_tokens": 1024, "temperature": 0, "thinking": False,
             "candidates": sorted(c.node for c in pack.candidates),
             "evidence_ids": sorted(evidence_catalog(pack)), "categories": sorted(load_contract().categories)}
    return hashlib.sha256(json.dumps(fixed, sort_keys=True).encode()).hexdigest()


def _compact(row, short_id):
    result = {"evidence_id": short_id, "node": row.get("node"), "kind": row["kind"]}
    for name in ("feature", "role", "category", "value", "reference", "minute", "dimensions",
                 "supported_category", "onset_minute", "recovery_minute", "source", "timestamp", "message"):
        if name in row:
            result[name] = row[name]
    if row["kind"] == "metric":
        for window in ("before", "during", "after"):
            value = row[window]
            result[window] = [value["observed"], value["expected"], value["median"], value["min"], value["max"]]
    return result


def _check_ids(values, allowed, minimum=1, maximum=6):
    if (not isinstance(values, list) or not minimum <= len(values) <= maximum or
            any(not isinstance(v, str) or v not in allowed for v in values) or len(set(values)) != len(values)):
        raise ValueError("invalid or repeated citations")


def _fit_evidence(material, rows, required=()):
    """Bound serialized observations before requesting an 8192-token server.

    This byte budget is conservative, not an exact tokenizer count. The server
    still enforces its context limit and its error is retained in the audit.
    """
    selected = [r for r in rows if r["evidence_id"] in required]
    def size(items):
        return len(json.dumps({**material, "evidence": items}, ensure_ascii=False).encode("utf-8"))
    if size(selected) > 12000:
        raise ValueError("required evidence exceeds compact context budget")
    seen = {r["evidence_id"] for r in selected}
    for row in rows:
        if row["evidence_id"] not in seen and size([*selected, row]) <= 12000:
            selected.append(row)
            seen.add(row["evidence_id"])
    return selected


def _call(complete, payload, validate, steps):
    stage = payload["response_format"]["json_schema"]["name"]
    for attempt in range(2):
        started = time.monotonic()
        detail = {"stage": stage, "attempt": attempt + 1}
        try:
            raw = complete(payload)
            choice = raw["choices"][0]
            content = choice["message"]["content"]
            detail.update(finish_reason=choice.get("finish_reason"), usage=raw.get("usage", {}),
                          response_excerpt=str(content)[:1500])
            if choice.get("finish_reason") == "length":
                raise ValueError("truncated_response")
            body = json.loads(content)
            validate(body)
            detail["status"] = "accepted"
            return body
        except HTTPError as exc:
            detail.update(status="request_failed", error="HTTPError", http_status=exc.code,
                          detail=exc.read(1500).decode("utf-8", "replace"))
            # An unsupported schema or bad request is not repaired by identical retries.
            raise
        except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
            detail.update(status="invalid_response", error=type(exc).__name__, detail=str(exc)[:1000])
            if attempt == 1 or isinstance(exc, OSError):
                raise
            payload = {**payload, "messages": [*payload["messages"], {
                "role": "user", "content": "Previous response failed validation: " + str(exc)[:300] +
                ". Produce a complete compact JSON object satisfying the schema. Do not repeat IDs."}]}
        finally:
            detail["seconds"] = round(time.monotonic() - started, 3)
            steps.append(detail)


def _diagnose(pack, complete, model, revision, steps):
    if not pack.device_context:
        raise ValueError("staged workflow requires evidence generated with --diagnostic-context")
    contract = load_contract()
    catalog = evidence_catalog(pack)
    if not catalog:
        raise ValueError("no observed evidence")
    ordered = sorted(catalog, key=lambda i: (not (catalog[i].get("role") == "direct" or
                                                 catalog[i].get("supported_category")), i))
    aliases = {item: f"E{n:03d}" for n, item in enumerate(ordered, 1)}
    actual = {v: k for k, v in aliases.items()}
    compact = {aliases[i]: _compact(catalog[i], aliases[i]) for i in ordered}
    nodes = sorted(c.node for c in pack.candidates)
    analyses = []
    note = {"type": "string", "maxLength": 300}
    for offset in range(0, len(nodes), 3):
        batch = nodes[offset:offset + 3]
        selected = []
        for node in batch:
            selected.extend([r for r in compact.values() if r["node"] == node][:10])
        material = {"nodes": batch, "start_time": pack.start_time, "end_time": pack.end_time,
                    "available_sources": pack.available_sources,
                    "metric_windows": "[observed, expected, median, min, max]; null means unknown"}
        # Interleave devices so a verbose first card cannot consume the whole budget.
        grouped = {node: [r for r in selected if r["node"] == node] for node in batch}
        selected = [grouped[node][i] for i in range(10) for node in batch if i < len(grouped[node])]
        selected = _fit_evidence(material, selected)
        ids = [r["evidence_id"] for r in selected]
        # Empty observations use a harmless enum sentinel, while validation requires zero citations.
        schema = _object({"devices": {"type": "array", "minItems": len(batch), "maxItems": len(batch),
            "items": _object({"node": {"type": "string", "enum": batch},
                "assessment": {"type": "string", "enum": _STATES},
                "evidence_ids": _citations(ids or ["NO_EVIDENCE"], 0, 4), "note": note})}})
        material["evidence"] = selected

        def validate(body):
            if not isinstance(body, dict) or set(body) != {"devices"} or not isinstance(body["devices"], list):
                raise ValueError("invalid device analysis")
            devices = body["devices"]
            if len(devices) != len(batch) or any(not isinstance(d, dict) for d in devices):
                raise ValueError("device coverage mismatch")
            names = [d.get("node") for d in devices]
            if any(not isinstance(n, str) for n in names) or sorted(names) != batch:
                raise ValueError("device coverage mismatch")
            for device in devices:
                if set(device) != {"node", "assessment", "evidence_ids", "note"} or device["assessment"] not in _STATES:
                    raise ValueError("invalid device assessment")
                if not isinstance(device["note"], str) or len(device["note"]) > 300:
                    raise ValueError("invalid device note")
                own = {r["evidence_id"] for r in selected if r["node"] == device["node"]}
                _check_ids(device["evidence_ids"], own, 0, 4)
                if device["assessment"] == "observed_anomaly" and not any(
                    has_device_support(catalog[actual[i]], device["node"]) for i in device["evidence_ids"]):
                    raise ValueError("anomaly lacks direct device evidence")
                observed = [r for r in selected if r["node"] == device["node"] and (
                    r["kind"] == "signal" or (r["kind"] == "metric" and r["during"][0] > 0))]
                if device["assessment"] == "no_supported_anomaly" and not observed:
                    raise ValueError("missing device evidence is unknown")

        result = _call(complete, _payload(model, "device_analysis", material, schema), validate, steps)
        analyses.extend(result["devices"])
    # Carry all cited observations and strongest direct/anomaly evidence into the ranking request.
    # Preserve probe symptoms too when many dimension anomalies would fill the budget.
    signal_ids = [aliases[i] for i in ordered if catalog[i]["kind"] == "signal"]
    cited_ids = [i for d in analyses for i in d["evidence_ids"]]
    ranking_ids = list(dict.fromkeys(cited_ids + signal_ids[:32] + list(compact)))[:64]
    material = {"candidates": nodes, "device_analyses": analyses,
                "start_time": pack.start_time, "end_time": pack.end_time,
                "traffic_observations": [{**r, "evidence_id": aliases[r["evidence_id"]]}
                                         for r in pack.traffic_observations if r["evidence_id"] in aliases]}
    evidence = _fit_evidence(material, [compact[i] for i in ranking_ids], required=cited_ids)
    ranking_ids = [r["evidence_id"] for r in evidence]
    material["evidence"] = evidence
    schema = _object({"roots": {"type": "array", "minItems": 5, "maxItems": 5,
                                "items": {"type": "string", "enum": nodes}},
                      "evidence_ids": _citations(ranking_ids), "note": note})

    def validate_rank(body):
        if not isinstance(body, dict) or set(body) != {"roots", "evidence_ids", "note"}:
            raise ValueError("invalid root ranking")
        roots = body["roots"]
        if (not isinstance(roots, list) or len(roots) != 5 or
                any(not isinstance(r, str) or r not in nodes for r in roots) or len(set(roots)) != 5):
            raise ValueError("invalid root ranking")
        _check_ids(body["evidence_ids"], ranking_ids)
        if not isinstance(body["note"], str) or len(body["note"]) > 300:
            raise ValueError("invalid ranking note")

    ranking = _call(complete, _payload(model, "root_ranking", material, schema), validate_rank, steps)
    rules = diagnose_rules(pack, contract)
    root_ids = [actual[i] for i in ranking["evidence_ids"]]
    proposal = {"event_id": pack.event_id, "evidence_sha256": pack.sha256(),
                "root_cause_top5": ranking["roots"],
                "fault_category": {"major_category": rules.category[0], "sub_category": rules.category[1]},
                "evidence_ids": root_ids}
    root_review = review_diagnosis(pack, rules, proposal, contract, policy="evidence-gated", application="roots-only")
    adopted = root_review.diagnosis.roots
    categories = ["/".join(c) for c in sorted(contract.categories)]
    material = {"adopted_roots": adopted, "allowed_categories": categories,
                "available_sources": pack.available_sources}
    relevant = sorted(compact.values(), key=lambda r: (r["node"] != adopted[0], r.get("role") != "symptom"))
    classification_evidence = _fit_evidence(material, relevant[:48])
    classification_ids = [r["evidence_id"] for r in classification_evidence]
    material["evidence"] = classification_evidence
    schema = _object({"category": {"type": "string", "enum": categories},
                      "evidence_ids": _citations(classification_ids), "note": note})

    def validate_category(body):
        if not isinstance(body, dict) or set(body) != {"category", "evidence_ids", "note"}:
            raise ValueError("invalid fault classification")
        if not isinstance(body["category"], str) or body["category"] not in categories:
            raise ValueError("invalid category pair")
        _check_ids(body["evidence_ids"], classification_ids)
        if not isinstance(body["note"], str) or len(body["note"]) > 300:
            raise ValueError("invalid classification note")

    classification = _call(complete, _payload(model, "fault_classification", material, schema), validate_category, steps)
    major, sub = classification["category"].split("/")
    category_ids = [actual[i] for i in classification["evidence_ids"]]
    response = {**proposal, "fault_category": {"major_category": major, "sub_category": sub},
                "evidence_ids": list(dict.fromkeys(root_ids + category_ids)),
                "root_evidence_ids": root_ids, "category_evidence_ids": category_ids,
                "workflow": "staged", "model": {"repository": model, "revision": revision},
                "prompt_sha256": workflow_fingerprint(pack, model)}
    _, error = _validate_diagnosis(pack, rules, response, contract)
    if error:
        raise ValueError(error)
    review = review_diagnosis(pack, rules, response, contract)
    return response, {"device_analyses": analyses, "ranking": ranking, "classification": classification,
                      "root_review": review.root_status, "category_review": review.category_status,
                      "adopted_roots": review.diagnosis.roots, "adopted_category": review.diagnosis.category}


def diagnose_staged(records, complete, model, revision):
    from .server_llm import _pack
    accepted, audits = [], []
    for record in records:
        steps = []
        audit = {"event_id": record.get("event_id"), "workflow": "staged", "steps": steps}
        started = time.monotonic()
        try:
            pack = _pack(record)
            if pack.sha256() != record["evidence_sha256"]:
                raise ValueError("evidence hash mismatch")
            response, analysis = _diagnose(pack, complete, model, revision, steps)
            accepted.append(response)
            audit.update(status="accepted", **analysis)
        except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
            audit.update(status="request_failed", error=type(exc).__name__, detail=str(exc)[:1000],
                         failed_stage=steps[-1]["stage"] if steps else "evidence")
            if steps and steps[-1].get("http_status"):
                audit.update(http_status=steps[-1]["http_status"], detail=steps[-1]["detail"])
        audit["seconds"] = round(time.monotonic() - started, 3)
        audits.append(audit)
    return accepted, audits

"""Validate server-produced diagnosis JSONL without loading a model on Mac."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .contracts import OfficialContract
from .diagnosis import Diagnosis, EvidencePack
from .evidence_catalog import evidence_catalog, has_category_support, has_device_support


def load_diagnoses(path: Path) -> dict[str, dict]:
    responses: dict[str, dict] = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or not isinstance(item.get("event_id"), str):
            continue
        event_id = item["event_id"]
        responses[event_id] = {"_duplicate": True} if event_id in responses else item
    return responses


def _validate_diagnosis(
    pack: EvidencePack,
    rules: Diagnosis,
    response: dict | None,
    contract: OfficialContract,
) -> tuple[Diagnosis, str | None]:
    if response is None:
        return rules, "missing_llm_response"
    allowed = {"event_id", "evidence_sha256", "root_cause_top5", "fault_category", "evidence_ids", "model", "prompt_sha256",
               "workflow", "root_evidence_ids", "category_evidence_ids"}
    if not isinstance(response, dict) or not {"event_id", "evidence_sha256", "root_cause_top5", "fault_category", "evidence_ids"} <= set(response) or set(response) - allowed:
        return rules, "invalid_llm_response"
    roots = response["root_cause_top5"]
    category = response["fault_category"]
    ids = response["evidence_ids"]
    candidates = {item.node for item in pack.candidates}
    available_ids = set(evidence_catalog(pack))
    if (
        response["event_id"] != pack.event_id
        or response["evidence_sha256"] != pack.sha256()
        or not isinstance(roots, list) or len(roots) != 5
        or any(not isinstance(root, str) or root not in candidates for root in roots)
        or len(set(roots)) != 5
        or not isinstance(category, dict) or set(category) != {"major_category", "sub_category"}
        or any(not isinstance(value, str) for value in category.values())
        or (category["major_category"], category["sub_category"]) not in contract.categories
        or not isinstance(ids, list) or not ids or any(not isinstance(item, str) or item not in available_ids for item in ids)
        or len(ids) > (12 if response.get("workflow") == "staged" else 6)
        or len(set(ids)) != len(ids)
    ):
        return rules, "invalid_llm_response"
    if not isinstance(response.get("workflow", "single"), str) or response.get("workflow", "single") not in {"single", "staged"}:
        return rules, "invalid_llm_response"
    for field in ("root_evidence_ids", "category_evidence_ids"):
        if field in response:
            values = response[field]
            if (not isinstance(values, list) or not values or len(values) > 6 or
                    any(not isinstance(v, str) or v not in ids for v in values) or
                    len(values) != len(set(values))):
                return rules, "invalid_llm_response"
    if response.get("workflow") == "staged" and not {"root_evidence_ids", "category_evidence_ids"} <= set(response):
        return rules, "invalid_llm_response"
    return Diagnosis(tuple(roots), (category["major_category"], category["sub_category"]), tuple(ids), "llm-jsonl"), None


@dataclass(frozen=True)
class DiagnosisReview:
    diagnosis: Diagnosis
    reason: str | None
    root_status: str
    category_status: str


def review_diagnosis(pack, rules, response, contract, *, policy="contract", application="both"):
    if policy not in {"contract", "evidence-gated"}:
        raise ValueError(f"unknown LLM policy: {policy}")
    if application not in {"both", "roots-only", "category-only"}:
        raise ValueError(f"unknown LLM application: {application}")
    proposed, error = _validate_diagnosis(pack, rules, response, contract)
    if error:
        return DiagnosisReview(rules, error, error, error)
    gated = policy == "evidence-gated" or response.get("workflow") == "staged"
    catalog = evidence_catalog(pack)
    root_ids = response.get("root_evidence_ids", response["evidence_ids"])
    category_ids = response.get("category_evidence_ids", response["evidence_ids"])
    roots, category = proposed.roots, proposed.category
    root_status = "unchanged" if roots == rules.roots else "accepted"
    category_status = "unchanged" if category == rules.category else "accepted"
    if application == "category-only":
        roots, root_status = rules.roots, "disabled"
    elif gated:
        changed = set(roots) - set(rules.roots)
        if roots[0] != rules.roots[0]:
            changed.add(roots[0])
        if any(not any(has_device_support(catalog[i], node) for i in root_ids) for node in changed):
            roots, root_status = rules.roots, "unsupported_root_change"
    if application == "roots-only":
        category, category_status = rules.category, "disabled"
    elif gated and category != rules.category:
        if not any(has_category_support(catalog[i], roots[0], category) for i in category_ids):
            category, category_status = rules.category, "unsupported_category_change"
    errors = [s for s in (root_status, category_status) if s.startswith("unsupported_")]
    diagnosis = Diagnosis(roots, category, proposed.evidence_ids, "llm-jsonl")
    if errors and roots == rules.roots and category == rules.category:
        diagnosis = rules
    return DiagnosisReview(diagnosis, ";".join(errors) or None, root_status, category_status)


def choose_diagnosis(pack, rules, response, contract, *, policy="contract", application="both"):
    review = review_diagnosis(pack, rules, response, contract, policy=policy, application=application)
    return review.diagnosis, review.reason

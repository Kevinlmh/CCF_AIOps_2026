"""Validate server-produced diagnosis JSONL without loading a model on Mac."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import OfficialContract
from .diagnosis import Diagnosis, EvidencePack


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


def choose_diagnosis(
    pack: EvidencePack,
    rules: Diagnosis,
    response: dict | None,
    contract: OfficialContract,
) -> tuple[Diagnosis, str | None]:
    if response is None:
        return rules, "missing_llm_response"
    allowed = {"event_id", "root_cause_top5", "fault_category", "evidence_ids", "model"}
    if not isinstance(response, dict) or not {"event_id", "root_cause_top5", "fault_category", "evidence_ids"} <= set(response) or set(response) - allowed:
        return rules, "invalid_llm_response"
    roots = response["root_cause_top5"]
    category = response["fault_category"]
    ids = response["evidence_ids"]
    candidates = {item.node for item in pack.candidates}
    available_ids = {item.evidence_id for item in pack.signals}
    if (
        response["event_id"] != pack.event_id
        or not isinstance(roots, list) or len(roots) != 5
        or any(not isinstance(root, str) or root not in candidates for root in roots)
        or len(set(roots)) != 5
        or not isinstance(category, dict) or set(category) != {"major_category", "sub_category"}
        or (category["major_category"], category["sub_category"]) not in contract.categories
        or not isinstance(ids, list) or not ids or any(not isinstance(item, str) or item not in available_ids for item in ids)
    ):
        return rules, "invalid_llm_response"
    return Diagnosis(tuple(roots), (category["major_category"], category["sub_category"]), tuple(ids), "llm-jsonl"), None

"""Apply validated four-role results to a rule diagnosis, field by field."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import OfficialContract
from .diagnosis import Diagnosis, EvidencePack
from .roles import ROLE_NAMES, validate


def load_roles(path: Path) -> dict[str, dict[str, dict]]:
    """Return ``event_id -> role -> body`` for accepted role responses only."""
    result: dict[str, dict[str, dict]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        event_id, role = row.get("event_id"), row.get("role")
        if role not in ROLE_NAMES or event_id is None:
            continue
        if row.get("status") != "accepted" or not isinstance(row.get("body"), dict):
            continue
        result.setdefault(event_id, {})[role] = row["body"]
    return result


def apply_roles(pack: EvidencePack, rules: Diagnosis, rows: dict[str, dict],
                contract: OfficialContract) -> tuple[Diagnosis, dict | None, list[str]]:
    """Return (diagnosis, boundary, fallback_fields).

    ``boundary`` is ``None`` unless the confirm role was accepted, the review
    role accepted it, and the proposed window passed validation.
    """
    fallbacks: list[str] = []
    bodies = dict(rows or {})
    confirm = bodies.get("confirm")
    confirm_ok = confirm is not None and validate(
        "confirm", pack, confirm, baseline=rules, upstream={}) is None
    if not confirm_ok:
        fallbacks.append("boundary")
    upstream = {"confirm": confirm} if confirm_ok else {}

    localize = bodies.get("localize")
    localize_ok = localize is not None and validate(
        "localize", pack, localize, baseline=rules, upstream=upstream) is None
    classify = bodies.get("classify")
    classify_ok = classify is not None and validate(
        "classify", pack, classify, baseline=rules,
        upstream={**upstream, "localize": localize} if localize_ok else upstream) is None

    review = bodies.get("review")
    review_ok = review is not None and validate(
        "review", pack, review, baseline=rules,
        upstream={"confirm": confirm if confirm_ok else {},
                  "localize": localize if localize_ok else {},
                  "classify": classify if classify_ok else {}}) is None

    def approved(field: str) -> bool:
        """A role validator is sufficient; a usable review can only veto."""
        if review is None or not review_ok:
            return True
        return review.get(field) == "accept"

    roots = rules.roots
    if localize_ok and approved("localize"):
        ranked = [item["network_element_id"] for item in localize["ranked"]]
        padded = list(ranked)
        for node in rules.roots:
            if len(padded) >= 5:
                break
            if node not in padded:
                padded.append(node)
        if len(padded) == 5:
            roots = tuple(padded)
        else:
            fallbacks.append("roots")
    else:
        fallbacks.append("roots")

    category = rules.category
    if classify_ok and approved("classify"):
        category = (classify["major_category"], classify["sub_category"])
    else:
        fallbacks.append("category")

    boundary = None
    if (confirm_ok and approved("confirm")
            and confirm.get("verdict") == "supported" and confirm.get("boundary")):
        boundary = {"start_time": confirm["boundary"]["start_time"],
                    "end_time": confirm["boundary"]["end_time"]}
    changed = roots != rules.roots or category != rules.category or boundary is not None
    return Diagnosis(roots, category, rules.evidence_ids,
                     "roles-jsonl" if changed else rules.source), boundary, fallbacks

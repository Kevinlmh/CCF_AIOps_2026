"""Strict JSONL schema validation shared by the evaluator and examples."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import load_public_config


class SchemaError(ValueError):
    """Raised when an input record does not conform to the public schema."""


_FAULT_TAXONOMY = load_public_config("fault_taxonomy")
_NETWORK_ELEMENTS = load_public_config("network_elements")
VALID_MAJOR = frozenset(_FAULT_TAXONOMY["major_categories"])
VALID_MAJOR_SUB_PAIRS = frozenset(
    (item["major_category"], item["sub_category"])
    for item in _FAULT_TAXONOMY["fault_categories"]
)
VALID_NETWORK_ELEMENTS = frozenset(
    f"{city}-{role}"
    for city in _NETWORK_ELEMENTS["cities"]
    for role in _NETWORK_ELEMENTS["device_roles"]
)
if len(_FAULT_TAXONOMY["fault_categories"]) != 32:
    raise RuntimeError("fault_taxonomy.json must contain the official 32 fault categories")
if VALID_MAJOR != {"link", "firewall", "resource", "routing", "service"}:
    raise RuntimeError("fault_taxonomy.json has unexpected major categories")


def parse_utc(value: Any, field: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{field} must be a non-empty ISO 8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SchemaError(f"{field} is not a valid ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise SchemaError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _object(record: Any, name: str, required: Iterable[str], allowed: Iterable[str]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise SchemaError(f"{name} must be an object")
    required_set, allowed_set = set(required), set(allowed)
    missing = required_set - record.keys()
    extra = set(record) - allowed_set
    if missing:
        raise SchemaError(f"{name} missing fields: {sorted(missing)}")
    if extra:
        raise SchemaError(f"{name} has unknown fields: {sorted(extra)}")
    return record


def _parse_top5(top5: Any, *, tolerate_invalid: bool = False, allow_duplicate_top5: bool = False) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(top5, list) or len(top5) > 5:
        if tolerate_invalid:
            return [], True
        raise SchemaError("root_cause_top5 must contain between zero and five entries")
    if not top5:
        return [], False

    normalized: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    invalid = False
    for expected_rank, cause in enumerate(top5, 1):
        try:
            cause = _object(
                cause,
                "root_cause_top5 entry",
                ("rank", "network_element_id"),
                ("rank", "network_element_id"),
            )
        except SchemaError:
            if tolerate_invalid:
                return [], True
            raise
        rank = cause["rank"]
        node = cause["network_element_id"]
        if type(rank) is not int or rank != expected_rank:
            if tolerate_invalid:
                return [], True
            raise SchemaError("root_cause_top5 ranks must be exactly 1 through len(top5)")
        if not isinstance(node, str) or not node:
            if tolerate_invalid:
                return [], True
            raise SchemaError("network_element_id must be a non-empty string")
        if node in seen_nodes:
            if not tolerate_invalid and not allow_duplicate_top5:
                raise SchemaError("root_cause_top5 cannot contain duplicate network_element_id")
            invalid = True
        if node not in VALID_NETWORK_ELEMENTS:
            if not tolerate_invalid:
                raise SchemaError(f"unknown network_element_id: {node!r}")
            invalid = True
        seen_nodes.add(node)
        normalized.append({"rank": rank, "network_element_id": node})
    return normalized, invalid


def _parse_category(category: Any, *, tolerate_invalid: bool = False) -> tuple[dict[str, str], bool, bool]:
    if tolerate_invalid:
        if not isinstance(category, dict):
            return {"major_category": "", "sub_category": ""}, True, True
        major = category.get("major_category")
        sub = category.get("sub_category")
        normalized_major = major if isinstance(major, str) and major else ""
        normalized_sub = sub if isinstance(sub, str) and sub else ""
        invalid_major = normalized_major not in VALID_MAJOR
        invalid_minor = (
            invalid_major
            or not normalized_sub
            or (normalized_major, normalized_sub) not in VALID_MAJOR_SUB_PAIRS
        )
        return {
            "major_category": normalized_major,
            "sub_category": normalized_sub,
        }, invalid_major, invalid_minor

    try:
        category = _object(
            category,
            "fault_category",
            ("major_category", "sub_category"),
            ("major_category", "sub_category"),
        )
        major = category["major_category"]
        sub = category["sub_category"]
        if not isinstance(major, str) or not major or not isinstance(sub, str) or not sub:
            raise SchemaError("fault categories must be non-empty strings")
    except SchemaError:
        raise

    invalid_major = major not in VALID_MAJOR
    invalid_minor = (major, sub) not in VALID_MAJOR_SUB_PAIRS
    if invalid_major or invalid_minor:
        if invalid_major:
            raise SchemaError(f"unknown major_category: {major!r}")
        raise SchemaError(f"invalid fault category pair: {(major, sub)!r}")
    return {"major_category": major, "sub_category": sub}, invalid_major, invalid_minor


def validate_prediction(
    record: Any,
    *,
    allow_duplicate_top5: bool = False,
    allow_invalid_rca: bool = False,
    allow_invalid_category: bool = False,
) -> dict[str, Any]:
    item = _object(
        record,
        "prediction",
        ("prediction_id", "start_time", "end_time", "root_cause_top5", "fault_category"),
        ("prediction_id", "start_time", "end_time", "root_cause_top5", "fault_category"),
    )
    if not isinstance(item["prediction_id"], str) or not item["prediction_id"]:
        raise SchemaError("prediction_id must be a non-empty string")
    start, end = parse_utc(item["start_time"], "start_time"), parse_utc(item["end_time"], "end_time")
    if end <= start:
        raise SchemaError("end_time must be after start_time")
    top5, invalid_rca = _parse_top5(
        item["root_cause_top5"],
        tolerate_invalid=allow_invalid_rca,
        allow_duplicate_top5=allow_duplicate_top5,
    )
    category, invalid_major, invalid_minor = _parse_category(
        item["fault_category"], tolerate_invalid=allow_invalid_category
    )
    result = {
        **item,
        "root_cause_top5": top5,
        "fault_category": category,
        "_start": start,
        "_end": end,
    }
    if invalid_rca:
        result["_invalid_rca"] = True
    if invalid_major:
        result["_invalid_major_category"] = True
    if invalid_minor:
        result["_invalid_minor_category"] = True
    return result


def normalize_prediction_for_evaluation(record: Any, line_number: int) -> dict[str, Any]:
    """Retain predictions while isolating invalid time, RCA, and category modules."""
    if not isinstance(record, dict):
        raise SchemaError(f"prediction line {line_number} must be an object")
    public = {key: value for key, value in record.items() if not key.startswith("_")}
    item = _object(
        public,
        "prediction",
        ("prediction_id",),
        ("prediction_id", "start_time", "end_time", "root_cause_top5", "fault_category"),
    )
    if not isinstance(item["prediction_id"], str) or not item["prediction_id"]:
        raise SchemaError("prediction_id must be a non-empty string")
    try:
        start = parse_utc(item.get("start_time"), "start_time")
        end = parse_utc(item.get("end_time"), "end_time")
        if end <= start:
            raise SchemaError("end_time must be after start_time")
        invalid_core = False
    except SchemaError:
        start, end = None, None
        invalid_core = True
    top5, invalid_rca = _parse_top5(item.get("root_cause_top5"), tolerate_invalid=True)
    category, invalid_major, invalid_minor = _parse_category(
        item.get("fault_category"), tolerate_invalid=True
    )
    normalized = {
        **item,
        "root_cause_top5": top5,
        "fault_category": category,
        "_start": start,
        "_end": end,
        "_invalid_core": invalid_core,
        "_evaluation_tolerant": True,
    }
    if invalid_rca:
        normalized["_invalid_rca"] = True
    if invalid_major:
        normalized["_invalid_major_category"] = True
    if invalid_minor:
        normalized["_invalid_minor_category"] = True
    return normalized


def load_predictions_for_evaluation(path: Path) -> list[dict[str, Any]]:
    """Load predictions while isolating only RCA and category failures."""
    records: list[dict[str, Any]] = []
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(f"cannot open {path}: {exc}") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SchemaError(f"{path}:{line_number}: malformed JSONL") from exc
            try:
                records.append(normalize_prediction_for_evaluation(raw, line_number))
            except SchemaError as exc:
                raise SchemaError(f"{path}:{line_number}: {exc}") from exc
    seen: set[str] = set()
    for record in records:
        original = record["prediction_id"]
        if original in seen:
            raise SchemaError(f"{path}: duplicate prediction_id: {original!r}")
        seen.add(original)
    return records


def validate_ground_truth(record: Any) -> dict[str, Any]:
    item = _object(
        record,
        "ground truth",
        ("ground_truth_id", "start_time", "end_time", "root_cause", "fault_category"),
        ("ground_truth_id", "start_time", "end_time", "root_cause", "fault_category"),
    )
    if not isinstance(item["ground_truth_id"], str) or not item["ground_truth_id"]:
        raise SchemaError("ground_truth_id must be a non-empty string")
    start, end = parse_utc(item["start_time"], "start_time"), parse_utc(item["end_time"], "end_time")
    if end <= start:
        raise SchemaError("end_time must be after start_time")
    root = _object(item["root_cause"], "root_cause", ("network_element_id",), ("network_element_id",))
    if not isinstance(root["network_element_id"], str) or not root["network_element_id"]:
        raise SchemaError("root_cause.network_element_id must be a non-empty string")
    if root["network_element_id"] not in VALID_NETWORK_ELEMENTS:
        raise SchemaError(f"unknown ground-truth network_element_id: {root['network_element_id']!r}")
    _parse_category(item["fault_category"])
    return {**item, "_start": start, "_end": end}


def load_jsonl(path: Path, validator: Callable[[Any], dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(f"cannot open {path}: {exc}") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SchemaError(f"{path}:{line_number}: malformed JSONL") from exc
            try:
                records.append(validator(raw))
            except SchemaError as exc:
                raise SchemaError(f"{path}:{line_number}: {exc}") from exc
    return records


def ensure_unique(records: list[dict[str, Any]], field: str) -> None:
    values = [record[field] for record in records]
    if len(values) != len(set(values)):
        raise SchemaError(f"{field} values must be unique")

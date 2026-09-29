"""Official identifiers and strict public prediction validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class OfficialContract:
    nodes: frozenset[str]
    categories: frozenset[tuple[str, str]]


def load_contract() -> OfficialContract:
    config = Path(__file__).parent / "config"
    elements = json.loads((config / "network_elements.json").read_text())
    taxonomy = json.loads((config / "fault_taxonomy.json").read_text())
    nodes = frozenset(
        f"{city}-{role}"
        for city in elements["cities"]
        for role in elements["device_roles"]
    )
    categories = frozenset(
        (entry["major_category"], entry["sub_category"])
        for entry in taxonomy["fault_categories"]
    )
    if len(nodes) != 80 or len(categories) != 28:
        raise ContractError("official configuration has wrong cardinality")
    return OfficialContract(nodes, categories)


def parse_time(raw: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ContractError("time must be nonempty ISO 8601 text")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"invalid time: {raw}") from exc
    if parsed.tzinfo is None:
        raise ContractError("time must include timezone")
    return parsed.astimezone(timezone.utc)


def validate_prediction(record: dict, contract: OfficialContract) -> None:
    expected = {"prediction_id", "start_time", "end_time", "root_cause_top5", "fault_category"}
    if not isinstance(record, dict) or set(record) != expected:
        raise ContractError("prediction fields do not match public contract")
    if not isinstance(record["prediction_id"], str) or not record["prediction_id"]:
        raise ContractError("prediction_id required")
    start, end = parse_time(record["start_time"]), parse_time(record["end_time"])
    if end <= start:
        raise ContractError("end_time must follow start_time")
    roots = record["root_cause_top5"]
    if not isinstance(roots, list) or len(roots) != 5:
        raise ContractError("exactly five root candidates required")
    seen: set[str] = set()
    for rank, root in enumerate(roots, 1):
        if not isinstance(root, dict) or set(root) != {"rank", "network_element_id"}:
            raise ContractError("invalid root entry")
        node = root["network_element_id"]
        if type(root["rank"]) is not int or root["rank"] != rank or node not in contract.nodes or node in seen:
            raise ContractError("invalid root rank or network element")
        seen.add(node)
    category = record["fault_category"]
    if not isinstance(category, dict) or set(category) != {"major_category", "sub_category"}:
        raise ContractError("invalid category fields")
    if (category["major_category"], category["sub_category"]) not in contract.categories:
        raise ContractError("invalid official fault category")

"""Merge separately inferred batches into one validated official JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import load_contract, parse_time, validate_prediction


def _official_row(record: dict) -> dict:
    """Write the public example's field and timestamp representation."""
    def timestamp(value: str) -> str:
        return parse_time(value).isoformat(timespec="milliseconds")

    return {
        "prediction_id": record["prediction_id"],
        "start_time": timestamp(record["start_time"]),
        "end_time": timestamp(record["end_time"]),
        "root_cause_top5": [
            {"rank": root["rank"], "network_element_id": root["network_element_id"]}
            for root in record["root_cause_top5"]
        ],
        "fault_category": {
            "major_category": record["fault_category"]["major_category"],
            "sub_category": record["fault_category"]["sub_category"],
        },
    }


def merge_predictions(inputs: list[Path], output: Path) -> int:
    if not inputs:
        raise ValueError("at least one prediction file is required")
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    contract = load_contract()
    rows: list[dict] = []
    seen_events: set[tuple] = set()
    for path in inputs:
        for line_number, line in enumerate(Path(path).read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                validate_prediction(item, contract)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            signature = (
                parse_time(item["start_time"]), parse_time(item["end_time"]),
                tuple(root["network_element_id"] for root in item["root_cause_top5"]),
                item["fault_category"]["major_category"], item["fault_category"]["sub_category"],
            )
            if signature in seen_events:
                raise ValueError(f"duplicate prediction across inputs: {path}:{line_number}")
            seen_events.add(signature)
            item["prediction_id"] = f"pred_{len(rows) + 1:06d}"
            rows.append(_official_row(item))
    if len({item["prediction_id"] for item in rows}) != len(rows):
        raise ValueError("duplicate merged prediction_id")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w") as handle:
        for item in rows:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge v3 batch predictions")
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(merge_predictions(args.input, args.output))


if __name__ == "__main__":
    main()

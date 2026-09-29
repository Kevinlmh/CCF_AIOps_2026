"""Merge separately inferred batches into one validated official JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import load_contract, parse_time, validate_prediction


def merge_predictions(inputs: list[Path], output: Path) -> int:
    if not inputs:
        raise ValueError("at least one prediction file is required")
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    contract = load_contract()
    rows: list[dict] = []
    for batch, path in enumerate(inputs, 1):
        for line_number, line in enumerate(Path(path).read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                validate_prediction(item, contract)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            item["prediction_id"] = f"v3-b{batch}-{item['prediction_id']}"
            rows.append(item)
    rows.sort(key=lambda item: parse_time(item["start_time"]))
    for previous, current in zip(rows, rows[1:]):
        if parse_time(current["start_time"]) < parse_time(previous["end_time"]):
            raise ValueError(f"overlap between {previous['prediction_id']} and {current['prediction_id']}")
    if len({item["prediction_id"] for item in rows}) != len(rows):
        raise ValueError("duplicate merged prediction_id")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w") as handle:
        for item in rows:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge v3 batch predictions")
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(merge_predictions(args.input, args.output))


if __name__ == "__main__":
    main()

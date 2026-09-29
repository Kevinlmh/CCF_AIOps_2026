"""Local implementation of the published 40/40/10/10 public evaluator.

Formulae and one-to-one Dice matching follow the official baseline evaluator
from the Apache-2.0 aiops-challenge-2026 repository. Full hidden test labels
are unavailable; use this module for public examples and known truth only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import load_contract, parse_time, validate_prediction


def _dice(a: dict, b: dict) -> float:
    start_a, end_a = parse_time(a["start_time"]), parse_time(a["end_time"])
    start_b, end_b = parse_time(b["start_time"]), parse_time(b["end_time"])
    overlap = max(0.0, (min(end_a, end_b) - max(start_a, start_b)).total_seconds())
    denominator = (end_a - start_a).total_seconds() + (end_b - start_b).total_seconds()
    return 2 * overlap / denominator if denominator > 0 else 0.0


def _hungarian_min(cost: list[list[float]]) -> list[tuple[int, int]]:
    if not cost:
        return []
    rows, columns = len(cost), len(cost[0])
    if rows > columns:
        return [(column, row) for row, column in _hungarian_min([list(col) for col in zip(*cost)])]
    u = [0.0] * (rows + 1)
    v = [0.0] * (columns + 1)
    matching = [0] * (columns + 1)
    predecessor = [0] * (columns + 1)
    for row in range(1, rows + 1):
        matching[0] = row
        column0 = 0
        minimum = [float("inf")] * (columns + 1)
        used = [False] * (columns + 1)
        while True:
            used[column0] = True
            row0 = matching[column0]
            delta = float("inf")
            column1 = 0
            for column in range(1, columns + 1):
                if used[column]:
                    continue
                current = cost[row0 - 1][column - 1] - u[row0] - v[column]
                if current < minimum[column]:
                    minimum[column] = current
                    predecessor[column] = column0
                if minimum[column] < delta:
                    delta = minimum[column]
                    column1 = column
            for column in range(columns + 1):
                u[matching[column]] += delta
                v[column] -= delta
                if used[column]:
                    minimum[column] -= delta
            column0 = column1
            if matching[column0] == 0:
                break
        while True:
            previous = predecessor[column0]
            matching[column0] = matching[previous]
            column0 = previous
            if column0 == 0:
                break
    return [(matching[column] - 1, column - 1) for column in range(1, columns + 1) if matching[column]]


def _matches(truths: list[dict], predictions: list[dict]) -> list[tuple[int, int, float]]:
    if not truths or not predictions:
        return []
    weights = [[_dice(truth, prediction) for prediction in predictions] for truth in truths]
    matrix = [[weight if weight >= .4 else 0.0 for weight in row] for row in weights]
    assignments = _hungarian_min([[-weight for weight in row] for row in matrix])
    return [(i, j, matrix[i][j]) for i, j in assignments if matrix[i][j] >= .4]


def evaluate_records(truths: list[dict], predictions: list[dict]) -> dict:
    contract = load_contract()
    for record in predictions:
        validate_prediction(record, contract)
    for record in truths:
        if record["root_cause"]["network_element_id"] not in contract.nodes:
            raise ValueError("ground truth has unknown root")
        pair = (record["fault_category"]["major_category"], record["fault_category"]["sub_category"])
        if pair not in contract.categories:
            raise ValueError("ground truth has unknown category")
        if parse_time(record["end_time"]) <= parse_time(record["start_time"]):
            raise ValueError("ground truth has invalid time")
    for records, field in ((truths, "ground_truth_id"), (predictions, "prediction_id")):
        ids = [record[field] for record in records]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate {field}")
    matches = _matches(truths, predictions)
    by_truth = {i: (j, weight) for i, j, weight in matches}
    matched_predictions = {j for _, j, _ in matches}
    tp, fp, fn = len(matches), len(predictions) - len(matches), len(truths) - len(matches)
    precision = tp / (tp + fp) if tp + fp else 0.0
    alpha_fp = .7 + .3 * precision
    ad_sum = rca_sum = major_sum = minor_sum = 0.0
    per_truth = []
    for index, truth in enumerate(truths):
        if index not in by_truth:
            per_truth.append({"ground_truth_id": truth["ground_truth_id"], "matched": False})
            continue
        pred_index, weight = by_truth[index]
        prediction = predictions[pred_index]
        start_delta = abs((parse_time(prediction["start_time"]) - parse_time(truth["start_time"])).total_seconds())
        end_delta = abs((parse_time(prediction["end_time"]) - parse_time(truth["end_time"])).total_seconds())
        time_score = max(0.0, 1 - (start_delta + end_delta) / 360.0)
        ad = .7 + .3 * time_score
        root = truth["root_cause"]["network_element_id"]
        rca = next((1 - (item["rank"] - 1) * .2 for item in prediction["root_cause_top5"] if item["network_element_id"] == root), 0.0)
        major = float(prediction["fault_category"]["major_category"] == truth["fault_category"]["major_category"])
        minor = float(major and prediction["fault_category"]["sub_category"] == truth["fault_category"]["sub_category"])
        ad_sum += ad
        rca_sum += rca
        major_sum += major
        minor_sum += minor
        per_truth.append({"ground_truth_id": truth["ground_truth_id"], "matched": True, "prediction_id": prediction["prediction_id"], "dice": weight, "ad": ad, "rca": rca, "major": major, "minor": minor})
    divisor = len(truths) or 1
    ad_score = ad_sum / divisor * alpha_fp * 40 if truths else 0.0
    rca_score = rca_sum / divisor * 40 if truths else 0.0
    major_score = major_sum / divisor * 10 if truths else 0.0
    minor_score = minor_sum / divisor * 10 if truths else 0.0
    return {
        "Total": ad_score + rca_score + major_score + minor_score,
        "AD": ad_score, "RCA": rca_score, "Major": major_score, "Minor": minor_score,
        "TP": tp, "FP": fp, "FN": fn, "precision": precision, "alpha_fp": alpha_fp,
        "per_ground_truth": per_truth,
        "unmatched_prediction_ids": [item["prediction_id"] for j, item in enumerate(predictions) if j not in matched_predictions],
    }


def evaluate_files(ground_truth: Path, predictions: Path, report: Path) -> dict:
    def read_jsonl(path: Path) -> list[dict]:
        return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]

    result = evaluate_records(read_jsonl(ground_truth), read_jsonl(predictions))
    report = Path(report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate v3 on known public ground truth")
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate_files(args.ground_truth, args.predictions, args.report)
    print(json.dumps({key: result[key] for key in ("Total", "AD", "RCA", "Major", "Minor", "TP", "FP", "FN")}, indent=2))


if __name__ == "__main__":
    main()

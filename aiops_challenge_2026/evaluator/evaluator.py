"""Reference evaluator implementing the published 40/40/10/10 score."""

from __future__ import annotations

from typing import Any

from ..schema import ensure_unique, normalize_prediction_for_evaluation, validate_ground_truth, validate_prediction
from .matcher import maximum_weight_matches
from .metrics import ad_single, rca_single


def evaluate(truth_records: list[dict[str, Any]], prediction_records: list[dict[str, Any]]) -> dict[str, Any]:
    def checked(item, validator, *, duplicate_top5_tolerated: bool = False):
        # CLI loading annotates records with parsed timestamps; validate the
        # public fields again while ignoring only those private annotations.
        if not isinstance(item, dict):
            return validator(item)
        if item.get("_evaluation_tolerant"):
            return item
        public = {key: value for key, value in item.items() if not key.startswith("_")}
        if duplicate_top5_tolerated and validator is validate_prediction:
            result = validator(public, allow_duplicate_top5=True)
            if result.pop("_top5_duplicate", False):
                result["_invalid_rca"] = True
            return result
        return validator(public)

    truths = [checked(item, validate_ground_truth) for item in truth_records]
    predictions = [
        item
        if isinstance(item, dict) and item.get("_evaluation_tolerant")
        else normalize_prediction_for_evaluation(item, index + 1)
        for index, item in enumerate(prediction_records)
    ]
    ensure_unique(truths, "ground_truth_id")
    ensure_unique(predictions, "prediction_id")
    matches = maximum_weight_matches(truths, predictions)
    by_truth = {i: (j, weight) for i, j, weight in matches}
    matched_predictions = {j for _, j, _ in matches}
    n_true = len(truths)
    tp = len(matches)
    fp = len(predictions) - tp
    fn = n_true - tp
    precision = tp / (tp + fp) if tp + fp else 0.0
    alpha_fp = 0.7 + 0.3 * precision
    per_gt = []
    ad_sum = rca_sum = major_sum = minor_sum = 0.0
    for index, truth in enumerate(truths):
        match = by_truth.get(index)
        if match is None:
            detail = {"ground_truth_id": truth["ground_truth_id"], "matched": False, "prediction_id": None, "dice": 0.0, "ad": 0.0, "rca": 0.0, "major": 0.0, "minor": 0.0}
        else:
            pred_index, weight = match
            prediction = predictions[pred_index]
            ad = ad_single(truth, prediction)
            rca = rca_single(truth, prediction)
            major = float(
                not prediction.get("_invalid_major_category", False)
                and prediction["fault_category"]["major_category"]
                == truth["fault_category"]["major_category"]
            )
            minor = float(
                major
                and not prediction.get("_invalid_minor_category", False)
                and prediction["fault_category"]["sub_category"]
                == truth["fault_category"]["sub_category"]
            )
            ad_sum += ad; rca_sum += rca; major_sum += major; minor_sum += minor
            detail = {"ground_truth_id": truth["ground_truth_id"], "matched": True, "prediction_id": prediction["prediction_id"], "dice": weight, "ad": ad, "rca": rca, "major": major, "minor": minor, "predicted_root_cause_top5": prediction["root_cause_top5"], "predicted_fault_category": prediction["fault_category"]}
        per_gt.append(detail)
    divisor = n_true if n_true else 1
    score_ad = (ad_sum / divisor) * alpha_fp * 40.0 if n_true else 0.0
    score_rca = rca_sum / divisor * 40.0 if n_true else 0.0
    score_major = major_sum / divisor * 10.0 if n_true else 0.0
    score_minor = minor_sum / divisor * 10.0 if n_true else 0.0
    return {"Total": score_ad + score_rca + score_major + score_minor, "AD": score_ad, "RCA": score_rca, "Major": score_major, "Minor": score_minor, "TP": tp, "FP": fp, "FN": fn, "precision": precision, "alpha_fp": alpha_fp, "per_ground_truth": per_gt, "unmatched_prediction_ids": [predictions[i]["prediction_id"] for i in range(len(predictions)) if i not in matched_predictions]}

"""Metric formulae for the 2026 challenge."""

from __future__ import annotations

from typing import Any

RANK_SCORES = {1: 1.0, 2: 0.8, 3: 0.6, 4: 0.4, 5: 0.2}


def ad_single(truth: dict[str, Any], prediction: dict[str, Any]) -> float:
    start_delta = abs((prediction["_start"] - truth["_start"]).total_seconds())
    end_delta = abs((prediction["_end"] - truth["_end"]).total_seconds())
    time_score = max(0.0, 1.0 - (start_delta + end_delta) / (2.0 * 180.0))
    return 0.7 + 0.3 * time_score


def rca_single(truth: dict[str, Any], prediction: dict[str, Any]) -> float:
    if prediction.get("_invalid_rca", False):
        return 0.0
    root = truth["root_cause"]["network_element_id"]
    for item in prediction["root_cause_top5"]:
        if item["network_element_id"] == root:
            return RANK_SCORES[item["rank"]]
    return 0.0

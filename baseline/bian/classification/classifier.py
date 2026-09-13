"""Closed-set taxonomy classification driven by model evidence."""

from __future__ import annotations

from typing import Any


def classification_taxonomy(config: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "major_category": category["major_category"],
            "sub_category": category["sub_category"],
        }
        for category in config["fault_categories"]
    ]


def unknown_category() -> dict[str, str]:
    return {"major_category": "unknown", "sub_category": "unknown"}


def validate_classification(value: Any, taxonomy: list[dict[str, str]]) -> dict[str, str]:
    if not isinstance(value, dict) or not {"major_category", "sub_category", "confidence"} <= set(value):
        raise ValueError("classification schema mismatch")
    if {value["major_category"], value["sub_category"]} - {item["major_category"] for item in taxonomy} - {item["sub_category"] for item in taxonomy}:
        raise ValueError("classification is outside the public taxonomy")
    if not any(item["major_category"] == value["major_category"] and item["sub_category"] == value["sub_category"] for item in taxonomy):
        raise ValueError("classification pair is outside the public taxonomy")
    if not isinstance(value["confidence"], (int, float)) or not 0 <= value["confidence"] <= 1:
        raise ValueError("classification confidence must be in [0,1]")
    return {"major_category": value["major_category"], "sub_category": value["sub_category"], "confidence": float(value["confidence"])}


def classify_with_llm(backend, *, top5: list[dict[str, Any]], context: dict[str, Any], taxonomy: list[dict[str, str]], rounds: int, max_new_tokens: int) -> tuple[dict[str, str], dict[str, Any]]:
    outputs = []
    top_nodes = {item["network_element_id"] for item in top5}
    observed_evidence = [item for item in context["candidates"] if item["node_id"] in top_nodes]
    observed_timeline = [item for item in context["timeline"] if item.get("node_id") in top_nodes]
    for round_index in range(1, rounds + 1):
        result = backend.generate_json(
            role="7B-B",
            prompt_name="classification",
            payload={
                "round": round_index,
                "taxonomy": taxonomy,
                "root_cause_top5": top5,
                "timeline": observed_timeline,
                "candidate_evidence": observed_evidence,
                "local_prototype_hint": context.get("prototype_hint"),
            },
            validator=lambda value: validate_classification(value, taxonomy),
            max_new_tokens=max_new_tokens,
        )
        outputs.append(result)
    counts = {}
    for result in outputs:
        key = (result["major_category"], result["sub_category"])
        counts[key] = counts.get(key, 0) + 1
    winner = sorted(counts, key=lambda key: (-counts[key], key))[0]
    return {"major_category": winner[0], "sub_category": winner[1]}, {"rounds": outputs, "consensus": {"major_category": winner[0], "sub_category": winner[1], "votes": counts[winner]}}

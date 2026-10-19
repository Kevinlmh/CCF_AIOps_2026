import json

from aiops_v3.rerank import apply_rerank, validate_rerank


def material():
    return {
        "_candidates": ["xian-br-1", "xian-service-vm-1", "xian-fw"],
        "_signal_index": {"node:104:xian-br-1:node.cpu_usage": "xian-br-1",
                          "node:104:xian-service-vm-1:node.cpu_usage": "xian-service-vm-1"},
        "device_observations": [
            {"network_element_id": "xian-br-1",
             "observations": [{"evidence_id": "node:104:xian-br-1:node.cpu_usage",
                               "feature": "node.cpu_usage", "minute": 104, "score": 7.0}]},
            {"network_element_id": "xian-service-vm-1",
             "observations": [{"evidence_id": "node:104:xian-service-vm-1:node.cpu_usage",
                               "feature": "node.cpu_usage", "minute": 106, "score": 5.0}]},
            {"network_element_id": "xian-fw", "observations": []},
        ],
        "netflow_observers": [],
    }


def event():
    return {"prediction_id": "pred_000001", "start_time": "2026-09-17T05:44:00.000+00:00",
            "end_time": "2026-09-17T05:49:00.000+00:00",
            "root_cause_top5": [{"rank": i + 1, "network_element_id": node}
                                for i, node in enumerate(
                                    ["xian-service-vm-1", "xian-br-1", "xian-fw", "xian-cr-1", "xian-br-2"])],
            "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"}}


def test_cross_device_and_unknown_evidence_rejected():
    m = material()
    body = {"ranked": [{"rank": 1, "network_element_id": "xian-br-1",
                        "evidence_ids": ["node:104:xian-service-vm-1:node.cpu_usage"],
                        "reason": "x"}]}
    assert validate_rerank(m, body) == "cross_device_evidence"
    body["ranked"][0]["evidence_ids"] = ["missing"]
    assert validate_rerank(m, body) == "unknown_evidence"


def test_apply_gate_replaces_weak_baseline_and_keeps_strong_one():
    m = material()
    body = {"ranked": [{"rank": 1, "network_element_id": "xian-br-1",
                        "evidence_ids": ["node:104:xian-br-1:node.cpu_usage"],
                        "reason": "direct cpu"}]}
    row, status = apply_rerank(event(), m, body)
    assert status == "reranked"
    assert row["root_cause_top5"][0]["network_element_id"] == "xian-br-1"
    assert row["fault_category"] == event()["fault_category"]
    body["ranked"] = [{"rank": 1, "network_element_id": "xian-fw", "evidence_ids": [],
                       "reason": "guess"}]
    row, status = apply_rerank(event(), m, body)
    assert status.startswith("fallback_invalid")

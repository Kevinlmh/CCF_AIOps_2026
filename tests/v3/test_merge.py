import json

import pytest

from aiops_v3.merge import merge_predictions


def record(start, end):
    return {
        "prediction_id": "v3-e000001", "start_time": start, "end_time": end,
        "root_cause_top5": [
            {"rank": index + 1, "network_element_id": node}
            for index, node in enumerate(("xian-br-1", "xian-br-2", "xian-cr-1", "xian-cr-2", "xian-fw"))
        ],
        "fault_category": {"major_category": "link", "sub_category": "loss"},
    }


def test_merge_namespaces_ids_and_orders_batches(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text(json.dumps(record("2026-08-19T04:00:00Z", "2026-08-19T04:05:00Z")) + "\n")
    second.write_text(json.dumps(record("2026-09-03T04:00:00Z", "2026-09-03T04:05:00Z")) + "\n")
    output = tmp_path / "combined.jsonl"
    assert merge_predictions([second, first], output) == 2
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["start_time"] < rows[1]["start_time"]
    assert len({row["prediction_id"] for row in rows}) == 2


def test_merge_allows_distinct_concurrent_events(tmp_path):
    paths = []
    for index, (start, end) in enumerate((("2026-08-19T04:00:00Z", "2026-08-19T04:05:00Z"), ("2026-08-19T04:04:00Z", "2026-08-19T04:09:00Z"))):
        path = tmp_path / f"batch{index}.jsonl"
        path.write_text(json.dumps(record(start, end)) + "\n")
        paths.append(path)
    paths[1].write_text(json.dumps({**record("2026-08-19T04:04:00Z", "2026-08-19T04:09:00Z"), "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"}}) + "\n")
    assert merge_predictions(paths, tmp_path / "combined.jsonl") == 2


def test_merge_rejects_exact_duplicate_event(tmp_path):
    paths = []
    for index in range(2):
        path = tmp_path / f"batch{index}.jsonl"
        path.write_text(json.dumps(record("2026-08-19T04:00:00Z", "2026-08-19T04:05:00Z")) + "\n")
        paths.append(path)
    with pytest.raises(ValueError, match="duplicate prediction"):
        merge_predictions(paths, tmp_path / "combined.jsonl")

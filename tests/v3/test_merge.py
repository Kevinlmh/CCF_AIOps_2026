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


def test_merge_numbers_ids_and_preserves_batch_order(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text(json.dumps(record("2026-08-19T04:00:00Z", "2026-08-19T04:05:00Z")) + "\n")
    second.write_text(json.dumps(record("2026-09-03T04:00:00Z", "2026-09-03T04:05:00Z")) + "\n")
    output = tmp_path / "combined.jsonl"
    assert merge_predictions([first, second], output) == 2
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["start_time"] < rows[1]["start_time"]
    assert [row["prediction_id"] for row in rows] == ["pred_000001", "pred_000002"]
    assert rows[0]["start_time"] == "2026-08-19T04:00:00.000+00:00"
    assert rows[0]["end_time"] == "2026-08-19T04:05:00.000+00:00"
    assert list(rows[0]) == ["prediction_id", "start_time", "end_time", "root_cause_top5", "fault_category"]
    assert list(rows[0]["root_cause_top5"][0]) == ["rank", "network_element_id"]


def test_merge_keeps_input_batch_sequence_even_with_earlier_second_timestamp(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text(json.dumps(record("2026-09-03T04:00:00Z", "2026-09-03T04:05:00Z")) + "\n")
    second.write_text(json.dumps(record("2026-08-19T04:00:00Z", "2026-08-19T04:05:00Z")) + "\n")
    output = tmp_path / "combined.jsonl"
    assert merge_predictions([first, second], output) == 2
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["start_time"] > rows[1]["start_time"]


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

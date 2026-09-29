from aiops_v3.evaluation import evaluate_records


def truth():
    return {
        "ground_truth_id": "gt-1", "start_time": "2026-07-28T12:00:00Z", "end_time": "2026-07-28T12:10:00Z",
        "root_cause": {"network_element_id": "xian-br-1"},
        "fault_category": {"major_category": "link", "sub_category": "loss"},
    }


def prediction(identifier="p-1"):
    return {
        "prediction_id": identifier, "start_time": "2026-07-28T12:00:00Z", "end_time": "2026-07-28T12:10:00Z",
        "root_cause_top5": [
            {"rank": rank, "network_element_id": node}
            for rank, node in enumerate(("xian-br-1", "xian-br-2", "xian-cr-1", "xian-cr-2", "xian-fw"), 1)
        ],
        "fault_category": {"major_category": "link", "sub_category": "loss"},
    }


def test_exact_single_public_case_scores_full_marks():
    report = evaluate_records([truth()], [prediction()])
    assert report["Total"] == 100
    assert (report["TP"], report["FP"], report["FN"]) == (1, 0, 0)


def test_extra_prediction_only_penalizes_detection_module():
    extra = prediction("p-2")
    extra["start_time"] = "2026-07-28T13:00:00Z"
    extra["end_time"] = "2026-07-28T13:10:00Z"
    report = evaluate_records([truth()], [prediction(), extra])
    assert report["AD"] == 34
    assert report["RCA"] == 40
    assert report["Total"] == 94
    assert report["FP"] == 1

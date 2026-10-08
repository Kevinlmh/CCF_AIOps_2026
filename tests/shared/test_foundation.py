"""Public parsing and evaluation behavior independent of any diagnosis version."""

import csv
from importlib import import_module
from importlib.util import find_spec
import json
from pathlib import Path

def _shared(name):
    assert find_spec("aiops_common") is not None, "shared CSV parsers have not been extracted"
    return import_module(f"aiops_common.data.{name}")


def _official(name):
    assert find_spec("aiops_challenge_2026") is not None, "official SDK has not been retained"
    return import_module(f"aiops_challenge_2026.{name}")


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _stream(root, profile="stage1"):
    return _shared("source").CanonicalObservationStream(
        root, aliases={"xian": "xian", "beida": "beida"},
        valid_roles=("br-1", "traffic-vm", "service-vm-1"), profile=profile,
    )


def test_csv_parser_preserves_observed_zero_and_missing_value(tmp_path):
    folder = tmp_path / "xian_window" / "processed"
    _write(folder / "node_metrics.csv", [
        {"timestamp": "2026-07-28T04:00:00Z", "node": "br1", "cpu_usage": "0"},
        {"timestamp": "2026-07-28T04:01:00Z", "node": "br1", "cpu_usage": ""},
    ])
    rows = list(_stream(tmp_path))
    assert [(row.node_id, row.metric, row.value) for row in rows] == [
        ("xian-br-1", "node.cpu_usage", 0.0),
    ]


def test_csv_parser_preserves_peer_identity_and_log_text(tmp_path):
    folder = tmp_path / "xian_window" / "processed"
    _write(folder / "routing_metrics.csv", [
        {"timestamp": "2026-07-28T04:00:00Z", "node": "br-1",
         "metric_name": "bgp_peer_up", "label": 'peer="2001:db8::2"', "value": "0"},
    ])
    _write(folder / "frr_syslog_events.csv", [
        {"event_time": "2026-07-28T04:00:15Z", "hostname": "br-1",
         "severity": "error", "program": "bgpd", "message": "BGP peer down"},
    ])
    stream = _stream(tmp_path)
    rows = list(stream)
    peer = next(row for row in rows if row.source == "routing")
    assert peer.value == 0
    assert dict(peer.dimensions)["peer"] == "2001:db8::2"
    assert stream.drain_text_events()[0].message == "BGP peer down"


def test_traffic_counter_deltas_and_reset(tmp_path):
    folder = tmp_path / "xian_window" / "processed"
    rows = []
    for minute, requests, errors in [(0, 100, 10), (1, 120, 15), (2, 4, 1)]:
        rows.append({
            "timestamp_utc": f"2026-07-28T04:0{minute}:00Z", "series_key": "probe-a",
            "flow_type": "dns", "source_region": "xian", "target_region": "beida",
            "dns_flow_requests_total": str(requests), "dns_flow_error_total": str(errors),
        })
    _write(folder / "traffic_flow_metrics.csv", rows)
    parsed = list(_stream(tmp_path))
    ratios = [row.value for row in parsed if row.metric == "traffic.dns.error_ratio"]
    assert ratios == [0.25, 0.25]
    assert len([row for row in parsed if row.metric == "traffic.dns.counter_reset"]) == 1


def test_stage2_csv_layout_does_not_require_missing_modalities(tmp_path):
    folder = tmp_path / "xian_window" / "xian"
    _write(folder / "node_metrics.csv", [
        {"timestamp": "2026-09-17T04:00:00Z", "node": "br-1", "cpu_usage": "20"},
    ])
    stream = _stream(tmp_path, profile="stage2")
    assert [row.source for row in stream] == ["node"]
    assert stream.stats.files_by_source == {"node": 1}


def _records():
    truth = {
        "ground_truth_id": "gt-1", "start_time": "2026-07-28T12:00:00Z",
        "end_time": "2026-07-28T13:00:00Z",
        "root_cause": {"network_element_id": "xian-br-1"},
        "fault_category": {"major_category": "routing", "sub_category": "route_loop"},
    }
    prediction = {
        "prediction_id": "p-1", "start_time": truth["start_time"], "end_time": truth["end_time"],
        "root_cause_top5": [{"rank": 1, "network_element_id": "xian-br-1"}],
        "fault_category": dict(truth["fault_category"]),
    }
    return truth, prediction


def test_current_official_taxonomy_and_prediction_format():
    schema = _official("schema")
    assert len(schema.VALID_NETWORK_ELEMENTS) == 80
    assert len(schema.VALID_MAJOR_SUB_PAIRS) == 32
    _, prediction = _records()
    # A one-hour prediction and one candidate are valid format choices;
    # neither imposes a fault-duration distribution on later batches.
    assert schema.validate_prediction(prediction)["prediction_id"] == "p-1"


def test_official_evaluator_scores_new_routing_class():
    truth, prediction = _records()
    report = _official("evaluator.evaluator").evaluate([truth], [prediction])
    assert report["Total"] == 100
    assert (report["TP"], report["FP"], report["FN"]) == (1, 0, 0)


def test_official_evaluator_only_penalizes_ad_for_false_positive():
    truth, prediction = _records()
    extra = {**prediction, "prediction_id": "p-2",
             "start_time": "2026-07-28T14:00:00Z", "end_time": "2026-07-28T15:00:00Z"}
    report = _official("evaluator.evaluator").evaluate([truth], [prediction, extra])
    assert report["AD"] == 34
    assert report["RCA"] == 40
    assert report["Total"] == 94


def test_official_evaluator_duplicate_roots_invalidate_rca_only():
    truth, prediction = _records()
    prediction["root_cause_top5"].append({"rank": 2, "network_element_id": "xian-br-1"})
    report = _official("evaluator.evaluator").evaluate([truth], [prediction])
    assert report["RCA"] == 0
    assert report["AD"] == 40
    assert report["Total"] == 60


def test_official_public_examples_still_score_full_marks():
    root = Path(__file__).resolve().parents[2]
    read = lambda path: [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    report = _official("evaluator.evaluator").evaluate(
        read(root / "sample/ground_truth.jsonl"), read(root / "examples/predictions.jsonl"),
    )
    assert report["Total"] == 100
    assert (report["TP"], report["FP"], report["FN"]) == (3, 0, 0)

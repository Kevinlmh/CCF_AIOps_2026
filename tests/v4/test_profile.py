"""Statistics distinguish observations, missing data and sampling limits."""

from importlib.util import find_spec

import pytest

from test_records import module, write_csv


def profile(root, **kwargs):
    assert find_spec("aiops_v4.data.profile") is not None, "dataset profiling is not implemented"
    return module("profile").profile_dataset(root, "stage2", **kwargs)


def test_numeric_statistics_do_not_fill_missing_or_clip_invalid_values(tmp_path):
    write_csv(tmp_path, "node_metrics.csv", [
        {"timestamp": f"2026-09-17T04:0{i}:00Z", "node": "br1", "cpu_usage": value}
        for i, value in enumerate(["0", "10", "20", "30", "", "broken", "Infinity"])
    ])
    result = profile(tmp_path)
    field = result["fields"]["node.cpu_usage"]
    assert result["rows_scanned"] == 7 and result["complete"]
    assert (field["count"], field["numeric_count"], field["missing_count"], field["zero_count"]) == (7, 4, 1, 1)
    assert field["non_numeric_count"] == field["nonfinite_count"] == 1
    assert (field["min"], field["max"], field["mean"], field["median"], field["mad"]) == (0, 30, 15, 15, 10)
    assert field["std"] == pytest.approx(11.180339887498949)
    assert field["p95"] == pytest.approx(28.5)
    assert result["quality_flags"]["invalid_numeric:cpu_usage"] == 1
    assert result["quality_flags"]["nonfinite_numeric:cpu_usage"] == 1


def test_routing_metrics_and_device_roles_are_not_mixed(tmp_path):
    write_csv(tmp_path, "routing_metrics.csv", [
        {"timestamp": "2026-09-17T04:00:00Z", "node": "br1", "metric_name": "bgp_peer_up", "value": "0"},
        {"timestamp": "2026-09-17T04:00:00Z", "node": "br1", "metric_name": "route_count", "value": "1000"},
    ])
    write_csv(tmp_path, "node_metrics.csv", [
        {"timestamp": "2026-09-17T04:00:00Z", "node": "br1", "cpu_usage": "10"},
        {"timestamp": "2026-09-17T04:00:00Z", "node": "service-vm-1", "cpu_usage": "90"},
    ])
    result = profile(tmp_path)
    assert result["metrics"]["routing.bgp_peer_up"]["mean"] == 0
    assert result["metrics"]["routing.route_count"]["mean"] == 1000
    assert result["role_metrics"]["node/br-1/cpu_usage"]["mean"] == 10
    assert result["role_metrics"]["node/service-vm-1/cpu_usage"]["mean"] == 90


def test_prefix_scan_and_reservoir_quantiles_have_explicit_scope(tmp_path):
    write_csv(tmp_path, "node_metrics.csv", [
        {"timestamp": f"2026-09-17T04:0{i}:00Z", "node": "br1", "cpu_usage": str(i)} for i in range(5)
    ])
    result = profile(tmp_path, max_rows_per_file=3, reservoir_size=2)
    assert result["mode"] == "prefix_sample" and not result["complete"]
    assert result["rows_scanned"] == 3 and result["files"][0]["complete"] is False
    field = result["fields"]["node.cpu_usage"]
    assert field["mean"] == 1 and field["numeric_count"] == 3
    assert field["quantile_sample_count"] == 2 and field["quantiles_exact"] is False
    again = profile(tmp_path, max_rows_per_file=3, reservoir_size=2)
    assert again["fields"] == result["fields"]
    assert len(result["files"][0]["parsed_records_sha256"]) == 64


def test_quality_report_exposes_recent_duplicates_out_of_order_and_bad_times(tmp_path):
    row = {"timestamp": "2026-09-17T04:01:00Z", "node": "br1", "cpu_usage": "10"}
    write_csv(tmp_path, "node_metrics.csv", [row, row,
        {**row, "timestamp": "2026-09-17T04:00:00Z"}, {**row, "timestamp": "bad"},
    ])
    result = profile(tmp_path)
    assert result["recent_duplicate_rows"] == 1
    assert result["out_of_order_rows"] == 1
    assert result["quality_flags"]["invalid_timestamp"] == 1
    assert result["start_time"] == "2026-09-17T04:00:00.000000Z"
    assert result["end_time"] == "2026-09-17T04:01:00.000000Z"


def test_high_cardinality_reports_tracking_limits(tmp_path):
    write_csv(tmp_path, "routing_metrics.csv", [
        {"timestamp": "2026-09-17T04:00:00Z", "node": "br1", "metric_name": f"metric_{i}", "value": "1"}
        for i in range(5)
    ])
    result = profile(tmp_path, max_groups=2, cardinality_limit=2)
    assert len(result["metrics"]) == 2
    assert result["dropped_metric_observations"] == 3
    assert result["fields"]["routing.metric_name"]["distinct_exact"] is False
    assert result["fields"]["routing.metric_name"]["distinct_count_lower_bound"] == 3


def test_empty_discovery_is_an_error_but_empty_source_file_is_counted(tmp_path):
    with pytest.raises(ValueError, match="recognized CSV"):
        profile(tmp_path)
    (tmp_path / "node_metrics.csv").write_text("")
    result = profile(tmp_path)
    assert result["files_scanned"] == 1 and result["rows_scanned"] == 0
    assert result["complete"] and result["sources"]["node"]["files"] == 1


def test_ambiguous_header_is_reported_as_incomplete_with_error(tmp_path):
    (tmp_path / "node_metrics.csv").write_text("timestamp,node,node\n2026-09-17T04:00:00Z,br1,br2\n")
    result = profile(tmp_path)
    assert not result["complete"] and len(result["errors"]) == 1
    assert result["rows_scanned"] == 0


@pytest.mark.parametrize("kwargs", [{"reservoir_size": 0}, {"max_rows_per_file": 0},
                                   {"max_groups": 0}, {"cardinality_limit": 0}])
def test_invalid_resource_limits_are_rejected(tmp_path, kwargs):
    write_csv(tmp_path, "node_metrics.csv", [{"timestamp": "2026-09-17T04:00:00Z", "node": "br1"}])
    with pytest.raises(ValueError):
        profile(tmp_path, **kwargs)

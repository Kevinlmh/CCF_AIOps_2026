from pathlib import Path

from baseline.bian.preprocessing.multisource import iter_source_files


def test_stage1_data_directories_are_discovered(tmp_path: Path):
    city = tmp_path / "chengdu_20260819040000_20260902040000"
    raw = city / "chengdu_20260819040000_20260902040000_data"
    raw.mkdir(parents=True)
    (raw / "node_metrics_20260819040000_20260902040000.csv").write_text("timestamp,node,cpu_usage\n")
    (raw / "traffic_flow_metrics.csv").write_text("id,timestamp_utc\n")
    files = list(iter_source_files(city))
    assert {source for source, _ in files} == {"node", "traffic"}


def test_processed_copy_takes_precedence_over_raw(tmp_path: Path):
    city = tmp_path / "xian_window"
    raw = city / "xian_window_data"
    processed = city / "processed"
    raw.mkdir(parents=True)
    processed.mkdir(parents=True)
    for directory in (raw, processed):
        (directory / "node_metrics.csv").write_text("timestamp,node,cpu_usage\n")
    files = list(iter_source_files(city))
    assert files == [("node", processed / "node_metrics.csv")]


def test_processed_precedence_does_not_depend_on_matching_file_name(tmp_path: Path):
    city = tmp_path / "xian_window"
    raw = city / "xian_window_data"
    processed = city / "processed"
    raw.mkdir(parents=True)
    processed.mkdir(parents=True)
    (raw / "node_metrics_20260819.csv").write_text("timestamp,node,cpu_usage\n")
    (processed / "node_metrics_normalized.csv").write_text("timestamp,node,cpu_usage\n")
    assert list(iter_source_files(city)) == [
        ("node", processed / "node_metrics_normalized.csv")
    ]

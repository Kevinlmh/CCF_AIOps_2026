"""Real SQLite round trips, batch boundaries and non-destructive publication."""

from importlib.util import find_spec

import pytest

from test_records import module, write_csv


def store():
    assert find_spec("aiops_v4.data.store") is not None, "evidence storage is not implemented"
    return module("store")


def create(root, database):
    write_csv(root, "node_metrics.csv", [
        {"timestamp": "2026-09-17T04:00:00.500Z", "node": "br1", "cpu_usage": "10"},
        {"timestamp": "2026-09-17T04:01:00Z", "node": "br2", "cpu_usage": "20"},
        {"timestamp": "invalid", "node": "mystery", "cpu_usage": ""},
    ])
    return store().ingest_dataset(root, "stage2", database)


def test_query_returns_raw_evidence_with_fractional_time_and_invalid_rows(tmp_path):
    database = tmp_path / "evidence.sqlite"
    report = create(tmp_path / "input", database)
    rows = store().query_records(database, "stage2", source="node",
                                 start="2026-09-17T12:00:00+08:00", end="2026-09-17T12:01:00+08:00")
    assert len(rows) == 1 and rows[0]["values"]["cpu_usage"] == 10
    assert rows[0]["timestamp"] == "2026-09-17T04:00:00.500000Z"
    assert rows[0]["raw"]["cpu_usage"] == "10"
    assert (rows[0]["line_start"], rows[0]["line_end"]) == (2, 2)
    all_rows = store().query_records(database, "stage2")
    assert len(all_rows) == report["rows_scanned"] == 3
    assert all_rows[-1]["timestamp"] is None
    assert "invalid_timestamp" in all_rows[-1]["quality_flags"]
    saved = store().read_store_profile(database)
    assert saved["batch"] == "stage2" and saved["mode"] == "full" and saved["complete"]


def test_batch_node_and_record_id_filters_do_not_leak_other_records(tmp_path):
    database = tmp_path / "evidence.sqlite"
    create(tmp_path / "input", database)
    assert store().query_records(database, "stage1") == []
    selected = store().query_records(database, "stage2", node_id="xian-br-2")
    assert len(selected) == 1 and selected[0]["values"]["cpu_usage"] == 20
    by_id = store().query_records(database, "stage2", record_id=selected[0]["record_id"])
    assert by_id == selected
    assert store().query_records(database, "stage2", limit=1)[0]["values"]["cpu_usage"] == 10


def test_long_log_text_round_trips_without_summarizing(tmp_path):
    root = tmp_path / "input"
    message = "detail\n" + "完整日志" * 200
    write_csv(root, "frr_syslog_events.csv", [{
        "event_time": "2026-09-17T04:00:00Z", "hostname": "br1", "severity": "error",
        "program": "bgpd", "message": message,
    }])
    database = tmp_path / "logs.sqlite"
    store().ingest_dataset(root, "stage1", database)
    row = store().query_records(database, "stage1", source="frr")[0]
    assert row["text"]["message"] == message and row["raw"]["message"] == message


def test_existing_database_is_not_overwritten(tmp_path):
    database = tmp_path / "evidence.sqlite"
    create(tmp_path / "input", database)
    before = database.read_bytes()
    with pytest.raises(FileExistsError):
        store().ingest_dataset(tmp_path / "input", "stage2", database)
    assert database.read_bytes() == before


def test_failed_ingest_does_not_publish_partial_database(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    (root / "node_metrics.csv").write_text("timestamp,node,node\n2026-09-17T04:00:00Z,br1,br2\n")
    database = tmp_path / "bad.sqlite"
    with pytest.raises(ValueError, match="scan errors"):
        store().ingest_dataset(root, "stage2", database)
    assert not database.exists()


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 1001}, {"start": "2026-09-17T04:00:00"},
    {"start": "2026-09-17T05:00:00Z", "end": "2026-09-17T04:00:00Z"}])
def test_invalid_query_budget_and_time_ranges_are_rejected(tmp_path, kwargs):
    database = tmp_path / "evidence.sqlite"
    create(tmp_path / "input", database)
    with pytest.raises(ValueError):
        store().query_records(database, "stage2", **kwargs)


def test_missing_database_is_not_created_by_read_only_query(tmp_path):
    database = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        store().query_records(database, "stage2")
    assert not database.exists()

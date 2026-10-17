"""Scoped native queries must filter groups before visiting large state stores."""
import sqlite3

import pytest

from test_records import write_csv
from aiops_v4.features.build import build_feature_store
from aiops_v4.states.build import build_state_store
from aiops_v4.states.store import StateStore


@pytest.fixture
def canonical_states(tmp_path):
    raw = tmp_path / "raw"
    write_csv(raw, "node_metrics.csv", [
        dict(timestamp=f"2026-09-17T04:{minute:02d}:00Z", node=role, cpu_usage="10")
        for role in ("br1", "br2") for minute in range(20)])
    write_csv(raw, "routing_metrics.csv", [
        dict(timestamp=f"2026-09-17T04:{minute:02d}:00Z", node=role,
             metric_name="bgp_peer_up", label=f'peer="192.0.2.{peer}"', value="1")
        for role in ("br1", "br2") for peer in (1, 2) for minute in range(20)])
    features = tmp_path / "dataset/features"
    states = tmp_path / "dataset/states"
    build_feature_store(raw, "stage2", features, workers=1, min_free_bytes=0,
                        naive_timezone="UTC", netflow_streaming=False)
    build_state_store(features, "stage2", states, workers=1, min_free_bytes=0,
                      reference_start="2026-09-17T04:00:00Z", reference_end="2026-09-17T04:12:00Z")
    database = states / "states.sqlite"
    with StateStore(database) as store:
        # Independent all-record reader has no filtering or paging SQL from
        # query_states. Fixture is only 120 rows, across six native groups.
        observations = list(store.rows("states"))
    assert len(observations) == 120
    return database, observations


@pytest.mark.parametrize("source", ["node", "routing", "frr"])
def test_actual_scoped_query_searches_native_groups_without_global_state_scan(
        canonical_states, monkeypatch, source):
    database, _ = canonical_states
    original_connect = sqlite3.connect
    plans = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.startswith("SELECT d.") and "ORDER BY d.start,g.source,g.state_group,d.id LIMIT ? OFFSET ?" in sql:
                plans.append(list(super().execute("EXPLAIN QUERY PLAN " + sql, parameters)))
            return super().execute(sql, parameters)

    def connect(path, *args, **kwargs):
        if str(path).split("?")[0].endswith("/states.sqlite"):
            kwargs["factory"] = ObservedConnection
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with StateStore(database) as store:
        result = store.query_states(entity_id="xian-br-1", source=source, limit=1)
    assert len(plans) == 1
    plan = plans[0]
    assert not any("SCAN d" in row[3] for row in plan), plan
    assert any("SEARCH d USING INDEX delta_group_time" in row[3] for row in plan), plan
    if source == "frr":
        assert result == []
    else:
        assert len(result) == 1 and result[0][0]["source"] == source
        assert result[0][0]["identity"]["entity_id"] == "xian-br-1"


@pytest.mark.parametrize("filters", [
    dict(entity_id="xian-br-1", source="node", limit=3, offset=2),
    dict(entity_id="xian-br-1", limit=7, offset=1),
    dict(source="routing", limit=5, offset=4),
    dict(entity_id="xian-br-1", source="frr", limit=2, offset=1),
    dict(entity_id="xian-br-1", source="node", start="2026-09-17T04:02:00Z", end="2026-09-17T04:04:00Z"),
    dict(entity_id="xian-br-1", source="node", end="2026-09-17T04:02:00Z", offset=1),
    dict(entity_id="xian-br-1", source="node", start="2026-09-17T04:17:00Z"),
    dict(entity_id="xian-br-1", source="routing", group_id="selected-routing-group",
         start="2026-09-17T04:02:00Z", end="2026-09-17T04:05:00Z", limit=1, offset=1),
])
def test_actual_native_query_preserves_filters_order_and_pagination(canonical_states, filters):
    from datetime import datetime
    database, observations = canonical_states
    params = dict(filters)
    if params.get("group_id") == "selected-routing-group":
        params["group_id"] = min(row["group_id"] for row in observations
                                 if row["source"] == "routing" and row["identity"]["entity_id"] == "xian-br-1")
    def time(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    expected = []
    for row in observations:
        if "entity_id" in params and row["identity"]["entity_id"] != params["entity_id"]:
            continue
        if "source" in params and row["source"] != params["source"]:
            continue
        if "group_id" in params and row["group_id"] != params["group_id"]:
            continue
        if "start" in params and time(row["end_time"]) <= time(params["start"]):
            continue
        if "end" in params and time(row["start_time"]) >= time(params["end"]):
            continue
        expected.append(row)
    expected.sort(key=lambda row: (row["start_time"], row["source"], row["group_id"], row["vector_id"]))
    offset, limit = params.get("offset", 0), params.get("limit", 100)
    expected = expected[offset:offset + limit]
    with StateStore(database) as store:
        actual = [row for row, _ in store.query_states(**params)]
    assert actual == expected
    if params.get("source") == "node" and "start" in params and "end" in params:
        assert [row["start_time"] for row in actual] == [
            "2026-09-17T04:02:00.000000Z", "2026-09-17T04:03:00.000000Z"]

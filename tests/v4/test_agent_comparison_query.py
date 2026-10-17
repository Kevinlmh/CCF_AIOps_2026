"""Real candidate preparation must avoid a database-wide suppression index."""
import sqlite3

import pytest

from test_records import write_csv
from aiops_v4.agents.contracts import load_manifest
from aiops_v4.agents.tools import EvidenceSession
from aiops_v4.evidence.index import query_evidence
from aiops_v4.evidence.linked import build_linked_evidence
from aiops_v4.features.build import build_feature_store
from aiops_v4.features.store import micros
from aiops_v4.states import build as state_build


def linked_case(tmp_path, monkeypatch, suppression):
    raw = tmp_path / "raw"
    write_csv(raw, "node_metrics.csv", [
        dict(timestamp=f"2026-09-17T04:{minute:02d}:00Z", node=role,
             cpu_usage="90" if minute in (12, 13) else "10")
        for role in ("br1", "br2") for minute in range(20)
    ])
    dataset = tmp_path / "dataset"
    features, states, evidence = (dataset / name for name in ("features", "states", "evidence"))
    build_feature_store(raw, "stage2", features, workers=1, min_free_bytes=0,
                        naive_timezone="UTC", netflow_streaming=False)
    original_seal = state_build.seal_layer

    def seal_fixture(directory, summary, **kwargs):
        # Set controlled flags before this temporary fixture is sealed. No
        # production artifacts or payload reconstruction methods are mocked.
        with sqlite3.connect(directory / "states.sqlite") as db:
            for minute, value in suppression.items():
                db.execute("UPDATE deltas SET suppressed=? WHERE start=?", (
                    value, micros(f"2026-09-17T04:{minute:02d}:00Z")))
        return original_seal(directory, summary, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(state_build, "seal_layer", seal_fixture)
        state_build.build_state_store(
            features, "stage2", states, workers=1, min_free_bytes=0,
            reference_start="2026-09-17T04:00:00Z", reference_end="2026-09-17T04:12:00Z")
    build_linked_evidence(states, "stage2", evidence, skip_raw=True,
                          observations_per_kind=2, min_free_bytes=0)
    database = evidence / "evidence.sqlite"
    packet = query_evidence(database, "stage2", "bundle", entity_id="xian-br-1")[0]
    session = EvidenceSession(database, "stage2", packet)
    manifest = load_manifest(database, "stage2", packet["bundle_id"])
    assert [row["start_time"] for row in manifest] == [
        "2026-09-17T04:12:00.000000Z", "2026-09-17T04:13:00.000000Z"]
    return database, session, manifest


def test_actual_prepare_candidates_does_not_build_global_suppression_index(tmp_path, monkeypatch):
    database, session, manifest = linked_case(tmp_path, monkeypatch, {10: None, 11: 0, 14: 0, 15: None})
    original_connect = sqlite3.connect
    plans = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.startswith("SELECT s.payload,m.payload FROM states") and "s.grp=?" in sql:
                plans.append(list(super().execute("EXPLAIN QUERY PLAN " + sql, parameters)))
            return super().execute(sql, parameters)

    def connect(path, *args, **kwargs):
        if str(path).split("?")[0].endswith("/evidence.sqlite"):
            kwargs["factory"] = ObservedConnection
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    session.prepare_candidates([row["vector_id"] for row in manifest])
    assert len(plans) == 2, plans
    for plan in plans:
        assert not any("AUTOMATIC" in row[3] for row in plan), plan
        assert not any("SCAN upstream_states.deltas" in row[3] for row in plan), plan
        assert any("SEARCH upstream_states.deltas USING INDEX delta_external_id" in row[3]
                   for row in plan), plan
    assert [row["start_time"] for row in session.packet["local_comparison_observations"]] == [
        "2026-09-17T04:11:00.000000Z", "2026-09-17T04:14:00.000000Z"]


@pytest.mark.parametrize("suppression, expected_minutes", [
    ({10: 0, 11: None, 14: 0, 15: None}, [11, 14]),
    ({10: None, 11: 1, 14: 1, 15: 0}, [10, 15]),
    ({10: 1, 11: 1, 14: 1, 15: 1}, []),
])
def test_prepare_candidates_preserves_suppression_and_comparison_boundaries(
        tmp_path, monkeypatch, suppression, expected_minutes):
    _, session, manifest = linked_case(tmp_path, monkeypatch, suppression)
    session.prepare_candidates([row["vector_id"] for row in manifest])
    comparisons = session.packet["local_comparison_observations"]
    assert [row["start_time"] for row in comparisons] == [
        f"2026-09-17T04:{minute:02d}:00.000000Z" for minute in expected_minutes]
    assert all(row["entity_id"] == "xian-br-1" for row in comparisons)
    assert all(row["group_id"] == manifest[0]["group_id"] for row in comparisons)
    assert all(row["candidate"] is False and row["scoring_status"] == "scored" for row in comparisons)
    # With both eligible minutes suppressed, the minute ending exactly at
    # start-120s and the one beginning exactly at end+120s remain excluded.
    assert all(row["start_time"] not in {
        "2026-09-17T04:09:00.000000Z", "2026-09-17T04:16:00.000000Z"} for row in comparisons)

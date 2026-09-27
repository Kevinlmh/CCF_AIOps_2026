from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from aiops_challenge_2026.config import load_public_config
from aiops_v2.contracts import EdgeKey
from aiops_v2.data.feature_store import FeatureStore, build_feature_store
from aiops_v2.data.source import CanonicalObservationStream
from aiops_v2.detection.direct_evidence import score_direct_evidence
from baseline.bian.preprocessing.observations import NumericObservation


START = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)


def _numeric(
    minute: int,
    *,
    source: str,
    node: str,
    metric: str,
    value: float,
    direction: str = "high",
    dimensions: tuple[tuple[str, str], ...] = (),
) -> NumericObservation:
    return NumericObservation(
        timestamp=START + timedelta(minutes=minute, seconds=10),
        source=source,
        node_id=node,
        related_node_ids=(),
        metric=metric,
        value=value,
        dimensions=dimensions,
        direction=direction,
    )


def test_feature_store_aggregates_values_and_preserves_missing_mask(tmp_path) -> None:
    observations = [
        _numeric(
            0,
            source="node",
            node="chengdu-br-1",
            metric="node.cpu_usage",
            value=10.0,
        ),
        _numeric(
            0,
            source="node",
            node="chengdu-br-1",
            metric="node.cpu_usage",
            value=25.0,
        ),
        _numeric(
            2,
            source="node",
            node="chengdu-br-1",
            metric="node.cpu_usage",
            value=15.0,
        ),
    ]

    store = build_feature_store(
        observations,
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    node = store.entities.node_index("chengdu-br-1")
    feature = store.features.index("node", "node.cpu_usage")
    assert store.node_values.shape == (3, 80, 1)
    assert float(store.node_values[0, node, feature]) == 25.0
    assert bool(store.node_mask[0, node, feature]) is True
    assert bool(store.node_mask[1, node, feature]) is False
    assert float(store.node_values[1, node, feature]) == 0.0
    assert store.start_time == START
    assert store.end_time == START + timedelta(minutes=2)


def test_feature_store_models_traffic_as_source_to_target_edges(tmp_path) -> None:
    common = (
        ("flow_type", "web"),
        ("target_region", "wuhan"),
        ("target_domain", "web.wuhan.aiops.local"),
    )
    observations = [
        _numeric(
            0,
            source="traffic",
            node="chengdu-traffic-vm",
            metric="traffic.web.error_ratio",
            value=0.2,
            dimensions=common,
        ),
        _numeric(
            0,
            source="traffic",
            node="xian-traffic-vm",
            metric="traffic.web.error_ratio",
            value=0.3,
            dimensions=common,
        ),
    ]

    store = build_feature_store(
        observations,
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    first = store.entities.edge_index(
        EdgeKey("chengdu-traffic-vm", "service-group:wuhan:web", "traffic")
    )
    second = store.entities.edge_index(
        EdgeKey("xian-traffic-vm", "service-group:wuhan:web", "traffic")
    )
    feature = store.features.index("edge", "traffic.web.error_ratio")
    assert store.edge_values.shape == (1, 2, 1)
    assert float(store.edge_values[0, first, feature]) == pytest.approx(0.2)
    assert float(store.edge_values[0, second, feature]) == pytest.approx(0.3)


def test_dimension_sidecar_keeps_interface_series_separate(tmp_path) -> None:
    observations = [
        _numeric(
            minute,
            source="interface",
            node="chengdu-br-1",
            metric="interface.rx_error_rate",
            value=value,
            direction="high",
            dimensions=(("interface_id", interface_id), ("if_role", "upstream")),
        )
        for minute in range(4)
        for interface_id, value in (("ens4", 0.0), ("ens5", 5.0))
    ]

    store = build_feature_store(
        observations,
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    series = list(store.iter_dimension_series(source="interface"))
    by_interface = {
        dict(item.dimensions)["interface_id"]: item.values.tolist()
        for item in series
    }
    assert by_interface == {"ens4": [0.0] * 4, "ens5": [5.0] * 4}
    assert store.manifest["format_version"] == 2


def test_routing_state_label_does_not_split_a_peer_timeseries(tmp_path) -> None:
    observations = [
        _numeric(
            minute,
            source="routing",
            node="beida-br-1",
            metric="routing.bgp_peer_up",
            value=0.0 if 25 <= minute < 31 else 1.0,
            direction="low",
            dimensions=(("peer", "fd00::1"), ("state", "Idle" if 25 <= minute < 31 else "Established")),
        )
        for minute in range(60)
    ]

    store = build_feature_store(
        observations,
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    series = list(store.iter_dimension_series(source="routing"))
    assert len(series) == 1
    assert series[0].dimensions == (("peer", "fd00::1"),)
    assert series[0].values.tolist() == [1.0] * 25 + [0.0] * 6 + [1.0] * 29


def test_dimension_series_can_trigger_without_being_masked_by_another_interface(tmp_path) -> None:
    observations = [
        _numeric(
            minute,
            source="interface",
            node="chengdu-br-1",
            metric="interface.rx_error_rate",
            value=(2.0 if interface_id == "ens4" and 30 <= minute < 36 else
                   0.0 if interface_id == "ens4" else 10.0),
            direction="high",
            dimensions=(("interface_id", interface_id), ("if_role", "upstream")),
        )
        for minute in range(70)
        for interface_id in ("ens4", "ens5")
    ]
    store = build_feature_store(
        observations,
        load_public_config("network_elements"),
        tmp_path / "store",
    )

    result = score_direct_evidence(store)

    node = store.entities.node_index("chengdu-br-1")
    assert result.node_probability[32, node] > 0.8
    assert result.node_probability[20, node] < 0.5


def test_text_evidence_round_trips_with_feature_store(tmp_path) -> None:
    from baseline.bian.preprocessing.observations import TextEvent

    class Observations(list):
        text_events = [
            TextEvent(
                timestamp=START,
                source="frr",
                node_id="chengdu-br-1",
                severity="err",
                program="bgpd",
                event_family="bgp",
                message="BGP peer went Down",
            )
        ]

    # A stream-like observation collection carries text events alongside numbers.
    store = build_feature_store(
        Observations(
            [
                _numeric(
                    0,
                    source="frr",
                    node="chengdu-br-1",
                    metric="frr.event_count",
                    value=1.0,
                    dimensions=(("event_family", "bgp"), ("severity", "err")),
                )
            ]
        ),
        load_public_config("network_elements"),
        tmp_path / "store_with_text",
    )

    text = list(store.iter_text_events())
    assert len(text) == 1
    assert text[0]["message"] == "BGP peer went Down"
    assert store.manifest["text_event_count"] == 1


def test_stream_text_is_incrementally_persisted_and_parse_audit_is_complete(tmp_path) -> None:
    config = load_public_config("network_elements")
    stream = CanonicalObservationStream(
        Path("tests/fixtures/multisource/case"),
        aliases={city: city for city in config["cities"]},
        valid_roles=config["device_roles"],
    )

    store = build_feature_store(
        stream,
        config,
        tmp_path / "stream_store",
    )

    text = list(store.iter_text_events())
    audit = store.manifest["parser_audit"]
    assert len(text) == 2
    assert stream.text_events == []
    assert store.manifest["text_event_count"] == len(text)
    assert audit["files_by_source"] == {name: 1 for name in (
        "frr", "interface", "netflow", "node", "routing", "scrape", "traffic"
    )}
    assert audit["bad_rows_by_source"] == {}
    assert audit["timestamp_ranges_by_source"]["node"]["first"].startswith("2026-")
    assert len(audit["file_audit"]) == 7


def test_parser_audit_counts_invalid_numeric_values(tmp_path) -> None:
    path = tmp_path / "chengdu_window" / "processed" / "node_metrics.csv"
    path.parent.mkdir(parents=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "node", "cpu_usage"])
        writer.writerow(["2026-08-19 04:00:00", "br-1", "20"])
        writer.writerow(["2026-08-19 04:01:00", "br-1", "invalid"])
    config = load_public_config("network_elements")
    stream = CanonicalObservationStream(
        tmp_path,
        aliases={city: city for city in config["cities"]},
        valid_roles=config["device_roles"],
    )

    store = build_feature_store(stream, config, tmp_path / "store")

    assert store.manifest["parser_audit"]["invalid_numeric_values_by_source"] == {"node": 1}
    file_audit = next(iter(store.manifest["parser_audit"]["file_audit"].values()))
    assert file_audit["rows"] == 2
    assert file_audit["invalid_numeric_values"] == 1


def test_v1_feature_store_remains_openable(tmp_path) -> None:
    path = tmp_path / "store"
    store = build_feature_store(
        [_numeric(0, source="node", node="beida-br-1", metric="node.cpu_usage", value=10.0)],
        load_public_config("network_elements"),
        path,
    )
    manifest = dict(store.manifest)
    manifest["format_version"] = 1
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    restored = FeatureStore.open(path)

    assert restored.manifest["format_version"] == 1
    assert list(restored.iter_dimension_series()) == []


def test_failed_build_removes_its_partial_outputs_and_allows_retry(tmp_path) -> None:
    broken = tmp_path / "broken" / "processed" / "node_metrics.csv"
    broken.parent.mkdir(parents=True)
    broken.write_text("node,cpu_usage\nbr-1,20\n", encoding="utf-8")
    config = load_public_config("network_elements")
    stream = CanonicalObservationStream(
        tmp_path / "broken",
        aliases={city: city for city in config["cities"]},
        valid_roles=config["device_roles"],
    )
    destination = tmp_path / "store"

    with pytest.raises(ValueError, match="required column"):
        build_feature_store(stream, config, destination)

    assert list(destination.iterdir()) == []
    restored = build_feature_store(
        [_numeric(0, source="node", node="beida-br-1", metric="node.cpu_usage", value=10.0)],
        config,
        destination,
    )
    assert restored.manifest["format_version"] == 2


def test_feature_store_round_trip_uses_memory_mapped_arrays(tmp_path) -> None:
    path = tmp_path / "store"
    build_feature_store(
        [
            _numeric(
                0,
                source="frr",
                node="beida-br-1",
                metric="frr.event_count",
                value=2.0,
                dimensions=(("event_family", "bgp"), ("severity", "warning")),
            )
        ],
        load_public_config("network_elements"),
        path,
    )

    restored = FeatureStore.open(path)

    assert isinstance(restored.log_values, np.memmap)
    assert restored.features.names("log") == ("frr.bgp.warning.count",)
    node = restored.entities.node_index("beida-br-1")
    assert float(restored.log_values[0, node, 0]) == 2.0
    assert restored.manifest["observation_count"] == 1


def test_feature_store_rejects_nonempty_destination(tmp_path) -> None:
    path = tmp_path / "store"
    path.mkdir()
    (path / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        build_feature_store([], load_public_config("network_elements"), path)

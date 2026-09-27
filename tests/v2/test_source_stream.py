from __future__ import annotations

import csv
from pathlib import Path

import pytest

from aiops_challenge_2026.config import load_public_config
from aiops_v2.data.source import CanonicalObservationStream


FIXTURE = Path("tests/fixtures/multisource/case")


def test_source_stream_reads_all_seven_sources_without_unknown_nodes() -> None:
    config = load_public_config("network_elements")
    stream = CanonicalObservationStream(
        FIXTURE,
        aliases={city: city for city in config["cities"]},
        valid_roles=config["device_roles"],
    )

    observations = list(stream)

    assert {item.source for item in observations} == {
        "node",
        "interface",
        "routing",
        "scrape",
        "traffic",
        "netflow",
        "frr",
    }
    assert all(item.node_id is not None for item in observations)
    assert not any(item.node_id == "xian-probe-vm" for item in observations)
    assert stream.stats.rows_by_source == {
        "node": 3,
        "interface": 2,
        "routing": 3,
        "scrape": 2,
        "traffic": 3,
        "netflow": 3,
        "frr": 1,
    }


def test_source_stream_derives_traffic_counter_rates_and_ratios() -> None:
    config = load_public_config("network_elements")
    stream = CanonicalObservationStream(
        FIXTURE,
        aliases={city: city for city in config["cities"]},
        valid_roles=config["device_roles"],
    )

    traffic = [item for item in stream if item.source == "traffic"]
    second_minute = [
        item
        for item in traffic
        if item.timestamp.minute == 1 and item.metric.endswith(("requests_rate", "success_ratio", "error_ratio"))
    ]
    values = {item.metric: item.value for item in second_minute}

    assert values["traffic.web.requests_rate"] == pytest.approx(30.0)
    assert values["traffic.web.success_ratio"] == pytest.approx(25.0 / 30.0)
    assert values["traffic.web.error_ratio"] == pytest.approx(5.0 / 30.0)


def test_source_stream_is_single_use_to_keep_statistics_unambiguous() -> None:
    config = load_public_config("network_elements")
    stream = CanonicalObservationStream(
        FIXTURE,
        aliases={city: city for city in config["cities"]},
        valid_roles=config["device_roles"],
    )

    list(stream)

    with pytest.raises(RuntimeError, match="single-use"):
        list(stream)


def test_stream_retains_frr_messages_and_scrape_error_text() -> None:
    config = load_public_config("network_elements")
    stream = CanonicalObservationStream(
        FIXTURE,
        aliases={city: city for city in config["cities"]},
        valid_roles=config["device_roles"],
    )

    list(stream)

    evidence = list(stream.text_events)
    assert {item.source for item in evidence} == {"frr", "scrape"}
    assert any("BGP peer" in item.message for item in evidence)
    assert any(item.message == "timeout" for item in evidence)


def test_stream_rejects_a_source_file_missing_required_columns(tmp_path: Path) -> None:
    path = tmp_path / "chengdu_window" / "processed" / "node_metrics.csv"
    path.parent.mkdir(parents=True)
    path.write_text("node,cpu_usage\nbr-1,10\n", encoding="utf-8")
    config = load_public_config("network_elements")
    stream = CanonicalObservationStream(
        tmp_path,
        aliases={city: city for city in config["cities"]},
        valid_roles=config["device_roles"],
    )

    with pytest.raises(ValueError, match="required column"):
        list(stream)


def test_traffic_duplicate_minute_does_not_corrupt_counter_baseline(tmp_path: Path) -> None:
    path = tmp_path / "chengdu_window" / "processed" / "traffic_flow_metrics.csv"
    path.parent.mkdir(parents=True)
    rows = [
        ("2026-08-19 04:00:00", 0),
        ("2026-08-19 04:01:00", 60),
        ("2026-08-19 04:01:00", 999),
        ("2026-08-19 04:02:00", 120),
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp_utc",
                "series_key",
                "flow_type",
                "source_region",
                "target_region",
                "dns_flow_requests_total",
            ]
        )
        for timestamp, requests in rows:
            writer.writerow(
                [timestamp, "series-1", "dns", "chengdu", "wuhan", requests]
            )
    config = load_public_config("network_elements")
    stream = CanonicalObservationStream(
        tmp_path,
        aliases={city: city for city in config["cities"]},
        valid_roles=config["device_roles"],
    )

    observations = list(stream)

    final_rate = [
        item.value
        for item in observations
        if item.metric == "traffic.dns.requests_rate" and item.timestamp.minute == 2
    ]
    assert final_rate == [60.0]
    assert stream.stats.out_of_order_rows_by_source == {"traffic": 1}

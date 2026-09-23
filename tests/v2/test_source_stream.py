from __future__ import annotations

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

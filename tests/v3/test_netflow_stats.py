"""Unit tests for the compact NetFlow service/observer statistics builder."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import aiops_v3.netflow_stats as netflow_stats
from aiops_v3.netflow_stats import (
    CITY_BY_DIGIT,
    SERVICE_NODE,
    SERVICES,
    build,
    load_stats,
)

START = datetime(2026, 10, 1, 4, 0, 0, tzinfo=timezone.utc)

HEADER = [
    "minute_utc", "region_code", "region", "node_key", "node", "node_type",
    "interface_id", "if_role", "collector_port", "protocol",
    "src_addr", "src_port", "dst_addr", "dst_port",
    "packets", "bytes", "flow_record_count", "first_seen", "last_seen",
]

CITY_DIGIT = {city: digit for digit, city in CITY_BY_DIGIT.items()}


def service_address(service: str) -> tuple[str, int]:
    digit, port = SERVICES[service]
    return f"fd00:{digit}:40::31", port


def observer_address(city: str) -> str:
    return f"fd00:{CITY_DIGIT[city]}:40::10"


def minute_text(minute: int) -> str:
    return (START + timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M:%S")


def base_row(**overrides: object) -> dict:
    row = {
        "minute_utc": minute_text(0),
        "region_code": "beida",
        "region": "ccf-aiops-北大",
        "node_key": "br1",
        "node": "BR-1-ccf-aiops-北大",
        "node_type": "BR",
        "interface_id": "ens4",
        "if_role": "\\N",
        "collector_port": "2059",
        "protocol": "17",
        "src_addr": observer_address("beida"),
        "src_port": "50000",
        "dst_addr": service_address("dns")[0],
        "dst_port": str(service_address("dns")[1]),
        "packets": "1",
        "bytes": "100",
        "flow_record_count": "1",
        "first_seen": "2026-10-01 03:59:34.813",
        "last_seen": "2026-10-01 04:00:34.813",
    }
    row.update(overrides)
    return row


def request_row(service: str, city: str, minute: int, *, packets: int,
                octets: int, **overrides: object) -> dict:
    target, port = service_address(service)
    values = {
        "minute_utc": minute_text(minute),
        "src_addr": observer_address(city),
        "src_port": "50000",
        "dst_addr": target,
        "dst_port": str(port),
        "packets": str(packets),
        "bytes": str(octets),
        "protocol": "17" if service == "dns" else "6",
    }
    values.update(overrides)
    return base_row(**values)


def response_row(service: str, city: str, minute: int, *, packets: int,
                 octets: int, **overrides: object) -> dict:
    target, port = service_address(service)
    values = {
        "minute_utc": minute_text(minute),
        "src_addr": target,
        "src_port": str(port),
        "dst_addr": observer_address(city),
        "dst_port": "50000",
        "packets": str(packets),
        "bytes": str(octets),
        "protocol": "17" if service == "dns" else "6",
    }
    values.update(overrides)
    return base_row(**values)


def write_csv(path: Path, rows: list[dict], *, quote_all: bool = False,
              crlf: bool = False, bom: bool = False) -> None:
    encoding = "utf-8-sig" if bom else "utf-8"
    terminator = "\r\n" if crlf else "\n"
    quoting = csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, quoting=quoting,
                                lineterminator=terminator)
        writer.writeheader()
        writer.writerows(rows)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_aggregates_service_request_response_by_observer(tmp_path: Path) -> None:
    source = tmp_path / "netflow.csv"
    write_csv(source, [
        request_row("dns", "beida", 0, packets=10, octets=1000),
        response_row("dns", "beida", 0, packets=3, octets=300),
        request_row("dns", "xian", 0, packets=20, octets=2000),
        response_row("dns", "xian", 0, packets=1, octets=100),
    ])
    output = tmp_path / "stats.jsonl"

    manifest = build([source], output, start_time=START, minute_count=5)
    rows = load_stats(output)

    assert [(row["service"], row["observer_city"]) for row in rows] == [
        ("dns", "beida"), ("dns", "xian"),
    ]
    assert [(row["req_pkts"], row["resp_pkts"]) for row in rows] == [
        (10.0, 3.0), (20.0, 1.0),
    ]
    assert [(row["req_bytes"], row["resp_bytes"]) for row in rows] == [
        (1000.0, 300.0), (2000.0, 100.0),
    ]
    assert {row["service_node"] for row in rows} == {SERVICE_NODE["dns"]}
    assert {row["minute"] for row in rows} == {0}
    assert manifest["rows"] == 2
    assert manifest["minutes"] == 1
    assert manifest["services"] == ["dns"]
    assert manifest["format"] == "v3-netflow-service-v1"
    assert manifest["skipped"] == {
        "out_of_range": 0, "unparseable_minute": 0,
        "malformed": 0, "unattributed": 0,
    }


def test_ignores_non_service_addresses_and_icmpv6(tmp_path: Path) -> None:
    target, port = service_address("dns")
    source = tmp_path / "netflow.csv"
    write_csv(source, [
        # A longer address that merely starts with the service target.
        request_row("dns", "beida", 0, packets=9, octets=900,
                    dst_addr=f"{target}:5201", dst_port="5201"),
        # A neighbour address in the same /64.
        request_row("dns", "beida", 0, packets=9, octets=900,
                    dst_addr="fd00:1:40::33", dst_port=str(port)),
        # Protocol 58 (ICMPv6) must not be treated as a service request.
        request_row("dns", "beida", 0, packets=9, octets=900, protocol="58"),
        # Ordinary client traffic.
        base_row(dst_addr="fd00:100:10::11", dst_port="179", protocol="6"),
    ])
    output = tmp_path / "stats.jsonl"

    manifest = build([source], output, start_time=START, minute_count=5)

    assert load_stats(output) == ()
    assert manifest["rows"] == 0
    assert manifest["minutes"] == 0
    assert manifest["services"] == []


def test_quoted_crlf_bom_csv_is_parsed(tmp_path: Path) -> None:
    source = tmp_path / "netflow.csv"
    write_csv(source, [
        request_row("web", "wuhan", 0, packets=5, octets=500),
        response_row("web", "wuhan", 0, packets=4, octets=400),
    ], quote_all=True, crlf=True, bom=True)

    raw = source.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw

    output = tmp_path / "stats.jsonl"
    build([source], output, start_time=START, minute_count=5)
    rows = load_stats(output)

    assert len(rows) == 1
    row = rows[0]
    assert row["service"] == "web"
    assert row["service_node"] == SERVICE_NODE["web"]
    assert row["observer_city"] == "wuhan"
    assert (row["req_pkts"], row["resp_pkts"]) == (5.0, 4.0)
    assert (row["req_bytes"], row["resp_bytes"]) == (500.0, 400.0)


def test_gap_minutes_are_not_fabricated(tmp_path: Path) -> None:
    source = tmp_path / "netflow.csv"
    write_csv(source, [
        request_row("auth", "beida", 0, packets=2, octets=20),
        response_row("auth", "beida", 0, packets=2, octets=20),
        request_row("auth", "beida", 2, packets=2, octets=20),
        response_row("auth", "beida", 2, packets=2, octets=20),
    ])
    output = tmp_path / "stats.jsonl"

    manifest = build([source], output, start_time=START, minute_count=5)
    rows = load_stats(output)

    assert [row["minute"] for row in rows] == [0, 2]
    assert {row["minute"] for row in rows} == {0, 2}
    assert manifest["minutes"] == 2
    assert manifest["rows"] == 2


def test_request_only_minute_keeps_zero_response_row(tmp_path: Path) -> None:
    source = tmp_path / "netflow.csv"
    write_csv(source, [
        request_row("dns", "shanghai", 1, packets=7, octets=700),
        # A later response must not backfill the earlier minute.
        response_row("dns", "shanghai", 2, packets=7, octets=700),
    ])
    output = tmp_path / "stats.jsonl"

    build([source], output, start_time=START, minute_count=5)
    rows = load_stats(output)

    assert [(row["minute"], row["req_pkts"], row["resp_pkts"]) for row in rows] == [
        (1, 7.0, 0.0), (2, 0.0, 7.0),
    ]


def test_out_of_range_and_unparseable_minutes_are_skipped_and_counted(
        tmp_path: Path) -> None:
    source = tmp_path / "netflow.csv"
    write_csv(source, [
        request_row("dns", "beida", 0, packets=3, octets=30),
        request_row("dns", "beida", -1, packets=3, octets=30),
        request_row("dns", "beida", 5, packets=3, octets=30),
        request_row("dns", "beida", 0, packets=3, octets=30,
                    minute_utc="not-a-timestamp"),
        request_row("dns", "beida", 0, packets=3, octets=30,
                    minute_utc="\\N"),
    ])
    output = tmp_path / "stats.jsonl"

    manifest = build([source], output, start_time=START, minute_count=5)
    rows = load_stats(output)

    assert [(row["minute"], row["req_pkts"]) for row in rows] == [(0, 3.0)]
    assert manifest["skipped"]["out_of_range"] == 2
    assert manifest["skipped"]["unparseable_minute"] == 2
    assert manifest["skipped"]["malformed"] == 0
    assert manifest["skipped"]["unattributed"] == 0

    # Manifest contract.
    assert manifest["format"] == "v3-netflow-service-v1"
    assert datetime.fromisoformat(manifest["start_time"]) == START
    assert manifest["minute_count"] == 5
    assert manifest["rows"] == len(rows) == 1
    assert manifest["minutes"] == 1
    assert manifest["services"] == ["dns"]
    assert manifest["input_files"] == [source.name]
    assert manifest["input_paths"] == [str(source)]
    expected_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["input_sha256"] == {source.name: expected_sha}
    assert manifest["code_sha256"] == hashlib.sha256(
        Path(netflow_stats.__file__).read_bytes()).hexdigest()

    manifest_path = Path(str(output) + ".manifest.json")
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest


def test_load_stats_returns_tuple_and_preserves_order(tmp_path: Path) -> None:
    source = tmp_path / "netflow.csv"
    write_csv(source, [
        request_row("dns", "xian", 1, packets=1, octets=10),
        request_row("dns", "beida", 0, packets=1, octets=10),
    ])
    output = tmp_path / "stats.jsonl"
    build([source], output, start_time=START, minute_count=3)

    rows = load_stats(output)
    assert isinstance(rows, tuple)
    assert [row["minute"] for row in rows] == [0, 1]
    assert [row["observer_city"] for row in rows] == ["beida", "xian"]
    assert set(rows[0]) == {
        "minute", "service", "service_node", "observer_city",
        "req_pkts", "resp_pkts", "req_bytes", "resp_bytes",
    }


def test_multiple_inputs_are_merged_and_hashed(tmp_path: Path) -> None:
    first = tmp_path / "a" / "netflow.csv"
    second = tmp_path / "b" / "netflow.csv"
    first.parent.mkdir()
    second.parent.mkdir()
    write_csv(first, [request_row("dns", "beida", 0, packets=1, octets=10)])
    write_csv(second, [response_row("dns", "beida", 0, packets=1, octets=10)])
    output = tmp_path / "stats.jsonl"

    manifest = build([first, second], output, start_time=START, minute_count=2)
    rows = load_stats(output)

    assert len(rows) == 1
    assert (rows[0]["req_pkts"], rows[0]["resp_pkts"]) == (1.0, 1.0)
    assert set(manifest["input_sha256"]) == {"a/netflow.csv", "b/netflow.csv"}
    assert manifest["input_sha256"]["a/netflow.csv"] == hashlib.sha256(
        first.read_bytes()).hexdigest()
    assert manifest["input_sha256"]["b/netflow.csv"] == hashlib.sha256(
        second.read_bytes()).hexdigest()

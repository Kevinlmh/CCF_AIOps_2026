"""Metric meaning, explicit time interpretation and auxiliary identities."""

from datetime import datetime, timezone
from importlib import import_module
from importlib.util import find_spec

import pytest

from test_records import read


def feature_module(name):
    assert find_spec('aiops_v4.features') is not None, 'window feature layer is not implemented'
    return import_module(f'aiops_v4.features.{name}')


def test_semantics_separate_percent_ratio_rates_counters_and_interval_counts():
    get = feature_module('semantics').metric_semantics
    assert get('node', 'cpu_usage')['unit'] == 'percent'
    assert get('node', 'memory_available_ratio')['unit'] == 'fraction'
    assert get('interface', 'rx_bytes_rate')['kind'] == 'gauge'
    assert get('interface', 'rx_bytes_rate')['unit'] == 'bytes/second'
    assert get('routing', 'ipv6_route_change_total')['kind'] == 'counter'
    assert get('routing', 'bgp_peer_count')['kind'] == 'gauge'
    assert get('routing', 'ipv6_route_count')['kind'] == 'gauge'
    assert get('traffic', 'web_flow_request_latency_seconds_sum')['kind'] == 'counter'
    assert get('netflow', 'bytes')['kind'] == 'interval_count'
    assert get('interface', 'carrier_changes')['kind'] == 'unknown'
    assert get('node', 'new_metric')['status'] == 'unverified'
    assert get('node', 'cpu_usage')['verified'] is False


def test_original_naive_time_honors_explicit_timezone_and_window_boundary(tmp_path):
    rows, _ = read(tmp_path, 'node_metrics.csv', [{
        'timestamp': '2026-09-17 12:00:00', 'node': 'br1', 'cpu_usage': '10',
    }])
    functions = feature_module('time')
    actual, flags = functions.observation_time(rows[0], 'Asia/Shanghai')
    assert actual == datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    assert 'naive_timezone:Asia/Shanghai' in flags
    assert functions.window_start(actual, 60) == actual
    next_point = actual.replace(second=59, microsecond=999999)
    assert functions.window_start(next_point, 60) == actual


def test_aware_time_uses_its_own_offset(tmp_path):
    rows, _ = read(tmp_path, 'node_metrics.csv', [{
        'timestamp': '2026-09-17T12:00:00+08:00', 'node': 'br1',
    }])
    actual, flags = feature_module('time').observation_time(rows[0], 'UTC')
    assert actual.hour == 4 and not flags


@pytest.mark.parametrize('value', ['2026-11-01 01:30:00', '2026-03-08 02:30:00'])
def test_ambiguous_or_nonexistent_local_times_are_rejected(tmp_path, value):
    rows, _ = read(tmp_path, 'node_metrics.csv', [{'timestamp': value, 'node': 'br1'}])
    with pytest.raises(ValueError, match='ambiguous|nonexistent'):
        feature_module('time').observation_time(rows[0], 'America/New_York')


def test_unknown_raw_entity_retains_auxiliary_identity_and_is_not_a_candidate(tmp_path):
    rows, _ = read(tmp_path, 'node_metrics.csv', [{
        'timestamp': '2026-09-17T04:00:00Z', 'node': 'probe-vm', 'cpu_usage': '10',
    }])
    auxiliary = feature_module('identity').entity_identity(rows[0])
    assert auxiliary['entity_id'] == 'aux:xian:probe-vm'
    assert auxiliary['role'] == 'probe-vm' and auxiliary['candidate'] is False
    assert auxiliary['network_element_id'] is None
    rows, _ = read(tmp_path, 'node_metrics.csv', [{
        'timestamp': '2026-09-17T04:00:00Z', 'node': 'br1',
    }])
    known = feature_module('identity').entity_identity(rows[0])
    assert known['entity_id'] == known['network_element_id'] == 'xian-br-1'
    assert known['candidate'] and known['role'] == 'br-1'

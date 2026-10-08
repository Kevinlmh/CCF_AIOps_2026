"""Literal multiview statistics and conservative counter transformations."""

import pytest

from test_records import read
from test_semantics import feature_module


def windows(root, filename, rows, **options):
    records, _ = read(root, filename, rows)
    series = feature_module('series')
    prepared = [series.prepare_record(record, 'UTC') for record in records]
    prepared.sort(key=lambda row: (row['series_key'], row['timestamp'], row['ref']['record_index']))
    return list(feature_module('aggregate').iter_windows(prepared, 'stage2', **options))


def test_gauge_windows_preserve_zero_missing_coverage_and_absent_windows(tmp_path):
    result = windows(tmp_path, 'node_metrics.csv', [
        {'timestamp': '2026-09-17T04:00:00Z', 'node': 'br1', 'cpu_usage': '0'},
        {'timestamp': '2026-09-17T04:00:30Z', 'node': 'br1', 'cpu_usage': '10'},
        {'timestamp': '2026-09-17T04:00:45Z', 'node': 'br1', 'cpu_usage': ''},
        {'timestamp': '2026-09-17T04:02:00Z', 'node': 'br1', 'cpu_usage': '20'},
    ], expected_step_seconds=15)
    assert len(result) == 2
    first = result[0]
    assert first['start_time'] == '2026-09-17T04:00:00.000000Z'
    assert first['end_time'] == '2026-09-17T04:01:00.000000Z'
    assert first['view'] == 'resource' and first['identity']['candidate']
    value = first['metrics']['cpu_usage']
    assert value['mean'] == value['std'] == 5
    assert value['first'] == value['min'] == 0 and value['last'] == value['max'] == 10
    assert value['missing_input_count'] == 1 and value['input_count'] == 3
    assert value['missing_fraction'] == pytest.approx(1/3)
    assert value['slope_per_second'] == pytest.approx(1/3)
    assert first['coverage']['observed_timestamps'] == 3
    assert first['coverage']['expected_points'] == 4
    assert first['coverage']['fraction'] == .75
    assert value['semantics']['unit'] == 'percent'


def test_counter_decrease_missing_and_long_gap_do_not_create_increments(tmp_path):
    times = ['04:00:00', '04:00:30', '04:01:00', '04:01:30', '04:02:00', '04:02:30', '04:06:00', '04:06:30']
    values = ['100', '130', '5', '20', '', '50', '100', '130']
    result = windows(tmp_path, 'routing_metrics.csv', [
        {'timestamp': f'2026-09-17T{time}Z', 'node': 'br1', 'metric_name': 'ipv6_route_change_total', 'label': '', 'value': value}
        for time,value in zip(times,values)
    ])
    stats = [row['metrics']['ipv6_route_change_total'] for row in result]
    assert len(stats) == 4
    assert stats[0]['delta_sum'] == 30 and stats[0]['rate_mean'] == 1
    assert stats[1]['counter_decrease_count'] == 1 and stats[1]['delta_sum'] == 15
    assert stats[1]['rate_mean'] == .5
    assert stats[2]['rate_mean'] is None and stats[2]['delta_sum'] is None
    assert stats[3]['counter_gap_count'] == 1 and stats[3]['delta_sum'] == 30
    assert any(ref['record_index'] == 2 for ref in result[1]['references'])


def test_equal_time_duplicates_are_distinct_from_conflicting_samples(tmp_path):
    result = windows(tmp_path, 'routing_metrics.csv', [
        {'timestamp': f'2026-09-17T04:0{minute}:00Z', 'node': 'br1', 'metric_name': 'ipv6_route_change_total', 'label': '', 'value': value}
        for minute,value in [(0,'10'),(0,'10'),(1,'40'),(1,'45'),(2,'70')]
    ])
    metric='ipv6_route_change_total'
    assert result[0]['metrics'][metric]['used_sample_count'] == 1
    assert result[0]['metrics'][metric]['duplicate_input_count'] == 1
    assert result[1]['metrics'][metric]['used_sample_count'] == 0
    assert result[1]['metrics'][metric]['conflicting_time_count'] == 1
    assert result[2]['metrics'][metric]['rate_mean'] is None


def test_routing_keeps_metric_and_full_peer_labels_separate(tmp_path):
    result = windows(tmp_path, 'routing_metrics.csv', [
        {'timestamp':'2026-09-17T04:00:00Z','node':'br1','metric_name':'bgp_peer_up',
         'label':f'peer="{peer}"','value':value} for peer,value in [('2001:db8::1','0'),('2001:db8::2','1')]
    ])
    assert len(result) == 2
    assert sorted(row['metrics']['bgp_peer_up']['mean'] for row in result) == [0,1]
    assert {row['dimensions']['peer'] for row in result} == {'2001:db8::1','2001:db8::2'}


def test_netflow_quantities_are_additive_and_references_are_bounded(tmp_path):
    result = windows(tmp_path,'netflow_5tuple_minute_readable.csv',[
        {'minute_utc':'2026-09-17T04:00:00Z','node_key':'br1','interface_id':'ens4','protocol':'6',
         'src_addr':f'2001:db8::{index}','dst_addr':'2001:db8::100','packets':'2','bytes':'100','flow_record_count':'1'}
        for index in range(25)
    ])
    assert len(result) == 1
    row=result[0]
    assert row['metrics']['bytes']['sum'] == 2500
    assert row['metrics']['packets']['sum'] == 50
    assert row['metrics']['bytes']['used_sample_count'] == row['record_count'] == 25
    assert row['metrics']['bytes']['delta_sum'] is None
    assert row['dimensions'] == {'interface_id':'ens4','protocol':'6'}
    assert len(row['references']) == 8 and row['references_truncated']


def test_inactive_traffic_columns_do_not_inflate_missingness(tmp_path):
    result = windows(tmp_path,'traffic_flow_metrics.csv',[
        {'timestamp_utc':'2026-09-17T04:00:00Z','source_region':'xian','target_region':'beida','flow_type':'web',
         'series_key':'a','web_flow_observed_qps':'0','dns_flow_requests_total':'\\N'}
    ])
    assert set(result[0]['metrics']) == {'web_flow_observed_qps'}
    assert result[0]['metrics']['web_flow_observed_qps']['mean'] == 0


def test_partial_bad_and_negative_counter_values_are_explicit(tmp_path):
    result=windows(tmp_path,'routing_metrics.csv',[
        {'timestamp':f'2026-09-17T04:00:{second:02d}Z','node':'br1','metric_name':'ipv6_route_change_total','label':'','value':value}
        for second,value in [(0,'1'),(10,'broken'),(20,'-1'),(30,'10')]
    ])
    metric=result[0]['metrics']['ipv6_route_change_total']
    assert metric['invalid_input_count'] == 2
    assert metric['rate_mean'] is None
    assert metric['used_sample_count'] == 2


def test_large_window_quantiles_are_explicitly_approximate(tmp_path):
    result=windows(tmp_path,'node_metrics.csv',[
        {'timestamp':f'2026-09-17T04:00:00.{index:06d}Z','node':'br1','cpu_usage':str(index)} for index in range(200)
    ])
    metric=result[0]['metrics']['cpu_usage']
    assert metric['quantile_sample_count']==128 and metric['quantiles_exact'] is False
    assert metric['mean']==99.5

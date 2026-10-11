import copy

from aiops_v4.agents.tools import compact_role_observation


def test_compact_observation_preserves_native_peer_dimensions_and_state():
    state = dict(vector_id='w1', source='routing', group_id='g1',
                 identity={'entity_id': 'xian-br-1'},
                 dimensions={'metric_name': 'bgp_peer_up', 'peer': 'fd00::2'})
    matrix = dict(features={'bgp_peer_up::last': dict(raw_value=0, unit='binary', semantic_status='inferred')},
                  rule_inputs={'bgp_peer_up': dict(last=0, min=0, mean=0, unit='binary', usable=True, kind='gauge')})
    before = copy.deepcopy((state, matrix))
    result = compact_role_observation(state, matrix)
    assert result['dimensions'] == state['dimensions']
    assert result['feature_values']['bgp_peer_up::last']['raw_value'] == 0
    assert result['rule_inputs']['bgp_peer_up']['min'] == 0
    assert (state, matrix) == before


def test_compact_observation_preserves_counter_rates_and_reset_quality():
    state = dict(vector_id='w2', source='traffic', identity={'entity_id': 'xian-traffic-vm'},
                 dimensions={'flow_type': 'dns', 'target_region': 'wuhan'})
    matrix = dict(features={'dns_flow_error_total::rate_mean': dict(raw_value=None, unit='count', semantic_status='inferred')},
        rule_inputs={'dns_flow_error_total': dict(kind='counter', unit='count', usable=True,
            delta_sum=None, rate_mean=None, counter_decrease_count=1, counter_gap_count=0)})
    result = compact_role_observation(state, matrix)
    assert 'dns_flow_error_total::rate_mean' in result['feature_values']
    assert result['feature_values']['dns_flow_error_total::rate_mean']['raw_value'] is None
    assert result['rule_inputs']['dns_flow_error_total']['counter_decrease_count'] == 1
    assert result['rule_inputs']['dns_flow_error_total']['delta_sum'] is None


def test_compact_netflow_retains_interval_sum_without_interpreting_it_as_rate():
    result = compact_role_observation(dict(vector_id='w3', source='netflow'), dict(
        features={'bytes::sum': dict(raw_value=4096, unit='bytes', semantic_status='inferred')}, rule_inputs={}))
    assert result['feature_values']['bytes::sum']['raw_value'] == 4096
    assert result['feature_values']['bytes::sum']['unit'] == 'bytes'


def test_compact_counter_rate_has_per_second_unit_on_old_sealed_inputs():
    result = compact_role_observation(dict(vector_id='old', source='traffic'), dict(
        features={'requests_total::rate_mean': dict(raw_value=2, unit='count', semantic_status='inferred')},
        rule_inputs={'requests_total': dict(kind='counter', unit='count', usable=True, rate_mean=2, delta_sum=120)}))
    assert result['feature_values']['requests_total::rate_mean']['unit'] == 'count/second'
    assert result['rule_inputs']['requests_total']['unit'] == 'count'
    assert result['rule_inputs']['requests_total']['rate_unit'] == 'count/second'


def test_compact_counter_rate_does_not_difference_its_unit_again():
    result = compact_role_observation(dict(vector_id='new', source='traffic'), dict(
        features={'requests_total::rate_mean': dict(raw_value=2, unit='count/second', semantic_status='inferred')},
        rule_inputs={'requests_total': dict(kind='counter', unit='count', rate_unit='count/second', usable=True)}))
    assert result['feature_values']['requests_total::rate_mean']['unit'] == 'count/second'
    assert result['rule_inputs']['requests_total']['rate_unit'] == 'count/second'


def test_compact_counter_unknown_unit_stays_unknown():
    result = compact_role_observation(dict(vector_id='unknown', source='traffic'), dict(
        features={'mystery_total::rate_mean': dict(raw_value=2, unit=None, semantic_status='unverified')},
        rule_inputs={'mystery_total': dict(kind='counter', unit=None, usable=True)}))
    assert result['feature_values']['mystery_total::rate_mean']['unit'] is None
    assert result['rule_inputs']['mystery_total']['rate_unit'] is None

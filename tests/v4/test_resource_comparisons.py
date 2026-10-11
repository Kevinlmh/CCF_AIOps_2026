import copy

import pytest

from aiops_v4.agents import tools


def observation(identifier, cpu, memory, io, source='node'):
    return dict(vector_id=identifier, source=source, group_id='g', entity_id='test-node',
                feature_values={
                    'cpu_usage::mean': dict(raw_value=cpu, unit='percent'),
                    'memory_available_ratio::mean': dict(raw_value=memory, unit='fraction'),
                    'disk_io_util::mean': dict(raw_value=io, unit='percent')})


def test_resource_comparison_exposes_all_observed_changes_without_fault_labels():
    rows = [observation('a', 25, .85, 0), observation('b', 30, .85, 10)]
    references = {'g': dict(vector_id='reference', cpu_usage=.5, memory_available_ratio=.93)}
    original = copy.deepcopy(rows)
    result = tools.resource_change_summary(rows, references)
    group = result['g']
    assert group['scope'] == 'delivered_candidate_sample_not_all_windows'
    assert group['window_ids'] == ['a', 'b']
    assert group['reference_interpretation'] == 'initial_observation_not_known_healthy'
    metrics = group['metrics']
    assert metrics['cpu_usage']['median'] == 27.5
    assert metrics['cpu_usage']['median_delta'] == 27
    assert metrics['memory_available_ratio']['median_delta'] == pytest.approx(-.08)
    assert metrics['disk_io_util']['reference'] is None
    assert metrics['disk_io_util']['median_delta'] is None
    assert 'fault_category' not in group and 'decision' not in group
    assert rows == original


def test_resource_comparison_skips_nonfinite_values_and_other_sources():
    rows = [observation('a', float('nan'), None, 99),
            observation('b', 1, .9, 0, 'interface')]
    group = tools.resource_change_summary(rows, {'g': dict(cpu_usage=0)})['g']
    assert group['window_ids'] == ['a']
    assert 'cpu_usage' not in group['metrics'] and 'memory_available_ratio' not in group['metrics']
    assert group['metrics']['disk_io_util']['median'] == 99

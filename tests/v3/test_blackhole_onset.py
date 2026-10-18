"""Guard against treating left-censored prefix absence as repeated incidents."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiops_v3.data.feature_store import build_feature_store
from aiops_v3.data.observations import NumericObservation
from aiops_v3.detection import DetectorSettings, detect_with_audit
from aiops_v3.run import run
from aiops_v3.store import open_store


def prefix_store(path, prefixes, cpu=None):
    network = json.loads((Path(__file__).parents[2] / 'aiops_v3/config/network_elements.json').read_text())
    start = datetime(2026, 10, 1, 4, tzinfo=timezone.utc)
    count = len(next(iter(prefixes.values())))
    observations = []
    for minute in range(count):
        for prefix, values in prefixes.items():
            if values[minute] is not None:
                observations.append(NumericObservation(
                    start + timedelta(minutes=minute), 'routing', 'xian-cr-1', (),
                    'routing.ipv6_route_exists', values[minute], (('prefix', prefix),), 'low'))
        if cpu is not None:
            observations.append(NumericObservation(
                start + timedelta(minutes=minute), 'node', 'xian-service-vm-1', (),
                'node.cpu_usage', cpu[minute], (), 'high'))
    return open_store(build_feature_store(observations, network, path).path)


def intervals(result):
    return [(event.start_minute, event.end_minute) for event in result.events]


def test_initial_missing_prefix_is_audited_without_repeated_blackhole(tmp_path):
    store = prefix_store(tmp_path/'store', {'a': [0]*65 + [1]*100})
    assert intervals(detect_with_audit(store)) == [(0, 30), (30, 60), (60, 65)]
    result = detect_with_audit(store, DetectorSettings(prefix_aware_blackhole=True))
    assert result.events == ()
    assert any(item['reason'] == 'blackhole_onset_unobserved'
               and item['prefix'] == 'a' and item['start_minute'] == 0
               and item['end_minute'] == 65 for item in result.rejected)


def test_later_prefix_drop_survives_another_prefix_initial_absence(tmp_path):
    store = prefix_store(tmp_path/'store', {
        'initially-missing': [0]*90 + [1]*60,
        'new-failure': [1]*20 + [0]*4 + [1]*126,
    })
    # The node minimum is already zero; the new prefix still has an observed onset.
    result = detect_with_audit(store, DetectorSettings(prefix_aware_blackhole=True))
    assert intervals(result) == [(20, 24)]
    assert {s.category for s in result.events[0].signals} == {'blackhole'}


def test_observation_gap_does_not_establish_a_prefix_failure_onset(tmp_path):
    store = prefix_store(tmp_path/'store', {
        'stable': [1]*100,
        'gapped': [1]*10 + [None]*5 + [0]*4 + [1]*81,
    })
    result = detect_with_audit(store, DetectorSettings(prefix_aware_blackhole=True))
    assert result.events == ()
    assert any(item['reason'] == 'blackhole_onset_unobserved'
               and item['start_minute'] == 15 for item in result.rejected)


def test_observed_long_prefix_failure_keeps_legal_duration_segments(tmp_path):
    store = prefix_store(tmp_path/'store', {'a': [1]*10 + [0]*65 + [1]*100})
    result = detect_with_audit(store, DetectorSettings(prefix_aware_blackhole=True))
    assert intervals(result) == [(10, 40), (40, 70), (70, 75)]


def test_unknown_missing_state_does_not_hide_cpu_incident(tmp_path):
    store = prefix_store(tmp_path/'store', {'a': [0]*40 + [1]*80},
                         cpu=[10]*20 + [80]*4 + [10]*96)
    result = detect_with_audit(store, DetectorSettings(prefix_aware_blackhole=True))
    assert intervals(result) == [(20, 24)]
    assert {s.category for s in result.events[0].signals} == {'cpu_pressure'}


def test_prefix_onset_option_refuses_missing_dimension_evidence(tmp_path):
    store = prefix_store(tmp_path/'store', {'a': [0]*10 + [1]*90})
    store.manifest.pop('dimension_series_count')
    with pytest.raises(ValueError, match='prefix'):
        detect_with_audit(store, DetectorSettings(prefix_aware_blackhole=True))


def test_run_records_prefix_guard_and_uses_existing_store(tmp_path):
    store = prefix_store(tmp_path/'store', {'a': [0]*10 + [1]*90})
    summary = run(store.path, tmp_path/'run', prefix_aware_blackhole=True)
    assert summary.event_count == 0
    manifest = json.loads((summary.output_dir/'run_manifest.json').read_text())
    assert manifest['detector_settings']['prefix_aware_blackhole'] is True
    assert manifest['rejected_events'][0]['reason'] == 'blackhole_onset_unobserved'

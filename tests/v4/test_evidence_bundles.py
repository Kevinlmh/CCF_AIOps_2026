"""Behavioral contracts for review associations, not fault confirmation."""
import importlib
import json
import pytest


def module(name):
    try:
        return importlib.import_module('aiops_v4.evidence.' + name)
    except ModuleNotFoundError:
        pytest.fail('missing evidence implementation: ' + name)


def event(i, start, end, entity='xian-br-1', batch='stage2'):
    return dict(event_id=str(i), batch=batch, identity={'entity_id': entity},
                start_time=f'2026-09-17T00:{start:02d}:00.000000Z',
                end_time=f'2026-09-17T00:{end:02d}:00.000000Z',
                references=[{'record_id': f'r{i}', 'source_file': 'a.csv', 'record_index': i + 1}],
                references_truncated=False)


def observation(i=0, candidate=True, status='scored'):
    return dict(vector_id=str(i), group_id='g', batch='stage2', source='node', view='node',
                identity={'entity_id': 'xian-br-1'}, dimensions={},
                start_time='2026-09-17T00:00:00.000000Z', end_time='2026-09-17T00:01:00.000000Z',
                candidate=candidate, scoring_status=status, references=[{'record_id': f'r{i}'}],
                references_truncated=False, quality_flags=[], quality_signals=[], drivers=[],
                features={'cpu|mean': {'value': 90, 'raw_value': 90}}, rule_inputs={'up': {'min': 0}})


def test_strict_overlap_keeps_touch_gap_and_entities_separate():
    rows = [event(0, 0, 2, 'aux:xian:router'), event(1, 0, 2), event(2, 1, 3),
            event(3, 2, 4), event(4, 4, 5), event(5, 6, 7)]
    bundles = list(module('association').iter_bundles(rows, 'stage2'))
    assert [b['event_ids'] for b in bundles] == [['0'], ['1', '2', '3'], ['4'], ['5']]
    assert bundles[1]['association'] == 'same_entity_strict_overlap'
    assert bundles[1]['needs_confirmation'] is True
    assert bundles[1]['end_time'] == rows[3]['end_time']


def test_bundle_bounds_preserve_total_and_deterministic_identity():
    rows = [event(i, 0, 1) for i in range(50)]
    # Input order is canonical entity/start/end/event_id.
    rows.sort(key=lambda r: r['event_id'])
    a = list(module('association').iter_bundles(iter(rows), 'stage2'))[0]
    b = list(module('association').iter_bundles(iter(rows), 'stage2'))[0]
    assert a == b
    assert a['event_count'] == 50
    assert len(a['event_ids']) == len(a['references']) == 32
    assert a['event_ids_truncated'] and a['references_truncated']


@pytest.mark.parametrize('rows', [[event(0, 0, 1, batch='other')], [event(0, 2, 1)],
                                 [event(1, 2, 3), event(0, 0, 1)]])
def test_association_rejects_wrong_batch_interval_and_order(rows):
    with pytest.raises(ValueError):
        list(module('association').iter_bundles(rows, 'stage2'))


def test_topology_requires_provenance_and_official_endpoints(tmp_path):
    path = tmp_path / 'topology.json'
    path.write_text(json.dumps({'provenance': 'reviewed diagram page 1', 'edges': [
        {'a': 'xian-br-1', 'b': 'xian-cr-1', 'relation': 'link'},
        {'a': 'xian-cr-1', 'b': 'xian-br-1', 'relation': 'link'}]}))
    loaded = module('association').load_topology(path)
    assert len(loaded['edges']) == 1
    assert loaded['provenance'] == 'reviewed diagram page 1'
    for change in [{'provenance': ''}, {'edges': [{'a': 'aux:xian:router', 'b': 'xian-br-1', 'relation': 'link'}]}]:
        content = dict(loaded, **change)
        path.write_text(json.dumps(content))
        with pytest.raises(ValueError):
            module('association').load_topology(path)


def test_counterevidence_excludes_unscorable_and_does_not_claim_normal():
    packet = module('packet')
    bundle = list(module('association').iter_bundles([event(0, 0, 1)], 'stage2'))[0]
    obs = [packet.compact_observation(observation(i, False, status), observation(i))
           for i, status in enumerate(['scored', 'blocked_quality', 'insufficient_reference'])]
    result = packet.make_packet(bundle, [], obs, obs[1:], {'sources': {'frr': {'files': 0, 'rows': 0}}}, [], {})
    assert len(result['observations_without_current_trigger']) == 1
    assert result['counterevidence_interpretation'] == 'scorable_without_current_trigger; not_known_normal'
    assert len(result['quality_context']) == 2
    assert 'source_unavailable:frr' in result['limitations']
    assert 'topology_unavailable' in result['limitations']


def test_packet_caps_and_full_native_grain_are_explicit():
    packet = module('packet')
    bundle = list(module('association').iter_bundles([event(0, 0, 1)], 'stage2'))[0]
    rows = []
    for i in range(40):
        state = observation(i)
        state['dimensions'] = {'interface': 'eth0', 'peer': 'xian-cr-1'}
        state['references'] = [{'record_id': f'r{i}-{j}'} for j in range(8)]
        rows.append(packet.compact_observation(state, state))
    result = packet.make_packet(bundle, rows, [], [],
                                {'scope': {'mode': 'prefix_sample', 'timezone_officially_confirmed': False}},
                                [{'bundle_id': str(i)} for i in range(12)], {'observations_per_kind': 8})
    assert len(result['support']) == 8 and len(result['relations']) == 8
    assert len(result['references']) == 32
    assert result['support'][0]['dimensions']['peer'] == 'xian-cr-1'
    assert result['support'][0]['rule_inputs']['up']['min'] == 0
    assert result['truncation']['support'] and result['truncation']['relations'] and result['truncation']['references']
    assert 'prefix_sample' in result['limitations']
    assert 'timezone_unverified' in result['limitations']
    assert 'raw_not_materialized' in result['limitations']

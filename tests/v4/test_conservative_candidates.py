import json
from state_helpers import artifacts, window
from aiops_v4.states.pipeline import discover_states


def select(tmp_path, values, **kwargs):
    source = artifacts(tmp_path / 'input', [window(i, value=value) for i, value in enumerate(values)])
    out = tmp_path / 'out'
    s = discover_states(source, 'stage2', out, candidate_policy='conservative',
                        reference_start='2026-09-17T04:00:00Z', reference_end='2026-09-17T04:12:00Z', **kwargs)
    return s, [json.loads(line) for line in (out/'states.jsonl').read_text().splitlines()], [json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]


def test_small_fluctuation_on_zero_floor_is_context_only(tmp_path):
    s, states, events = select(tmp_path, [0]*12 + [0.02]*3)
    assert not events and s['suppressed_candidate_windows'] == 3
    assert states[-1]['raw_candidate'] and not states[-1]['candidate']
    assert states[-1]['suppression_reason'] == 'small_effect'


def test_singleton_weak_spike_is_not_an_event(tmp_path):
    s, states, events = select(tmp_path, [0]*12 + [100, 0])
    assert not events and states[12]['suppression_reason'] == 'insufficient_persistence'


def test_persistent_material_spike_keeps_original_interval_and_ownership(tmp_path):
    s, states, events = select(tmp_path, [0]*12 + [100, 100, 0])
    assert len(events) == 1 and events[0]['window_ids'] == ['w12', 'w13']
    assert s['candidate_windows'] == sum(e['window_count'] for e in events) == 2
    assert not states[-1]['candidate']


def test_direct_protocol_interruption_keeps_singleton(tmp_path):
    row = window(0, source='routing', metric='bgp_peer_up', value=0)
    row['metrics']['bgp_peer_up']['semantics']['unit'] = 'binary'
    source = artifacts(tmp_path/'input', [row]); out = tmp_path/'out'
    s = discover_states(source, 'stage2', out, candidate_policy='conservative')
    assert s['candidate_events'] == s['candidate_windows'] == 1


def test_netflow_is_queryable_context_and_not_standalone_candidate(tmp_path):
    rows = [window(i, source='netflow', metric='bytes', value=0 if i < 12 else 1000) for i in range(15)]
    source = artifacts(tmp_path/'input', rows); out = tmp_path/'out'
    s = discover_states(source, 'stage2', out, candidate_policy='conservative',
                        reference_start='2026-09-17T04:00:00Z', reference_end='2026-09-17T04:12:00Z')
    assert not s['candidate_events'] and s['states'] == 15


def test_interface_throughput_change_and_low_resource_utilization_are_context(tmp_path):
    # Many-fold traffic growth and ordinary disk activity are not failure facts.
    rows=[]
    for source,metric,unit,values in [('interface','tx_bytes_rate','bytes/second',[10]*12+[10000]*3),
                                     ('node','disk_write_rate','bytes/second',[10]*12+[10000]*3),
                                     ('node','cpu_usage','percent',[0]*12+[30]*3)]:
        for i,value in enumerate(values):
            w=window(i,source=source,metric=metric,value=value)
            w['window_id']=source+'-'+metric+'-'+str(i)
            if metric=='disk_write_rate':
                w['identity'].update(entity_id='xian-br-2',network_element_id='xian-br-2')
            w['metrics'][metric]['semantics']['unit']=unit
            rows.append(w)
    source=artifacts(tmp_path/'input',rows);out=tmp_path/'out'
    s=discover_states(source,'stage2',out,candidate_policy='conservative',
                      reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z')
    assert s['candidate_events']==0 and s['suppressed_candidate_windows']>=9


def test_memory_pressure_preserves_low_available_ratio_direction(tmp_path):
    rows=[]
    for i,value in enumerate([.8]*12+[.05,.05,.8]):
        w=window(i,metric='memory_available_ratio',value=value)
        w['metrics']['memory_available_ratio']['semantics']['unit']='fraction'
        rows.append(w)
    source=artifacts(tmp_path/'input',rows);out=tmp_path/'out'
    s=discover_states(source,'stage2',out,candidate_policy='conservative',
                      reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z')
    assert s['candidate_events']==1 and s['candidate_windows']==2


def test_constant_high_pressure_cannot_be_absorbed_by_reference(tmp_path):
    source=artifacts(tmp_path/'input',[window(i,value=95) for i in range(15)])
    s=discover_states(source,'stage2',tmp_path/'out',candidate_policy='conservative')
    assert s['candidate_events']==1 and s['candidate_windows']==15


def test_memory_decline_against_initial_observation_survives_majority_shift(tmp_path):
    rows=[]
    for i,value in enumerate([.9]*3+[.75]*13+[.9]*3):
        w=window(i,metric='memory_available_ratio',value=value)
        w['metrics']['memory_available_ratio']['semantics']['unit']='fraction'
        rows.append(w)
    source=artifacts(tmp_path/'input',rows);out=tmp_path/'out'
    s=discover_states(source,'stage2',out,candidate_policy='conservative')
    states=[json.loads(line) for line in (out/'states.jsonl').read_text().splitlines()]
    assert s['candidate_events']==1 and s['candidate_windows']==13
    assert states[3]['admission_reference']['memory_available_ratio']==.9


def test_static_zero_protocol_has_no_observed_interruption(tmp_path):
    rows=[window(i,source='routing',metric='bgp_peer_up',value=0) for i in range(15)]
    for row in rows:row['metrics']['bgp_peer_up']['semantics']['unit']='binary'
    source=artifacts(tmp_path/'input',rows);out=tmp_path/'out'
    s=discover_states(source,'stage2',out,candidate_policy='conservative')
    states=[json.loads(line) for line in (out/'states.jsonl').read_text().splitlines()]
    assert s['candidate_events']==0
    assert all(row['suppression_reason']=='static_protocol_zero' for row in states)


def test_rules_disabled_does_not_seed_constant_pressure(tmp_path):
    source=artifacts(tmp_path/'input',[window(i,value=95) for i in range(15)])
    s=discover_states(source,'stage2',tmp_path/'out',candidate_policy='conservative',rules_enabled=False)
    assert s['candidate_events']==0


def test_partial_cpu_pressure_needs_joint_rise_in_cpu_and_load(tmp_path):
    rows=[]
    for i in range(15):
        w=window(i,value=1 if i<3 else 25)
        import copy
        load=copy.deepcopy(w['metrics']['cpu_usage']);load['semantics'].update(unit='load',metric='load1')
        load.update(mean=.05 if i<3 else 1.5,last=.05 if i<3 else 1.5)
        w['metrics']['load1']=load;rows.append(w)
    source=artifacts(tmp_path/'input',rows);out=tmp_path/'out'
    s=discover_states(source,'stage2',out,candidate_policy='conservative')
    assert s['candidate_events']==1 and s['candidate_windows']==12


def test_five_percentage_point_memory_decline_is_a_cue_not_a_label(tmp_path):
    rows=[]
    for i,value in enumerate([.9]*3+[.84]*12):
        w=window(i,metric='memory_available_ratio',value=value)
        w['metrics']['memory_available_ratio']['semantics']['unit']='fraction';rows.append(w)
    source=artifacts(tmp_path/'input',rows);out=tmp_path/'out'
    s=discover_states(source,'stage2',out,candidate_policy='conservative')
    assert s['candidate_events']==1 and s['candidate_windows']==12


def test_ospf_observed_loss_of_full_state_keeps_singleton_and_not_recovery(tmp_path):
    rows = [window(i, source='routing', metric='ospf6_neighbor_state_code', value=v)
            for i, v in enumerate([8]*12 + [5, 8])]
    for row in rows:
        row['metrics']['ospf6_neighbor_state_code']['semantics']['unit'] = 'state_code'
    source = artifacts(tmp_path/'input', rows)
    out = tmp_path/'out'
    s = discover_states(source, 'stage2', out, candidate_policy='conservative')
    states = [json.loads(line) for line in (out/'states.jsonl').read_text().splitlines()]
    assert s['candidate_windows'] == 1 and s['candidate_events'] == 1
    assert states[12]['candidate'] and not states[13]['candidate']


def test_static_nonfull_ospf_is_context_when_no_full_state_observed(tmp_path):
    rows = [window(i, source='routing', metric='ospf6_neighbor_state_code', value=5) for i in range(15)]
    for row in rows:
        row['metrics']['ospf6_neighbor_state_code']['semantics']['unit'] = 'state_code'
    source = artifacts(tmp_path/'input', rows)
    assert discover_states(source, 'stage2', tmp_path/'out', candidate_policy='conservative')['candidate_events'] == 0


def test_ospf_nonfull_observation_within_recovered_window_is_not_lost(tmp_path):
    rows=[window(i,source='routing',metric='ospf6_neighbor_state_code',value=8) for i in range(14)]
    for row in rows:row['metrics']['ospf6_neighbor_state_code']['semantics']['unit']='state_code'
    rows[12]['metrics']['ospf6_neighbor_state_code'].update(min=5,change_count=2)
    source=artifacts(tmp_path/'input',rows)
    assert discover_states(source,'stage2',tmp_path/'out',candidate_policy='conservative')['candidate_windows']==1


def test_invalid_initial_resource_values_do_not_create_relative_pressure(tmp_path):
    import copy
    rows=[]
    for i in range(15):
        w=window(i, value=-100 if i==0 else 1)
        load=copy.deepcopy(w['metrics']['cpu_usage'])
        load['semantics'].update(metric='load1', unit='load')
        load.update(mean=-20 if i==0 else .05, last=-20 if i==0 else .05)
        memory=copy.deepcopy(w['metrics']['cpu_usage'])
        memory['semantics'].update(metric='memory_available_ratio',unit='fraction')
        memory.update(mean=2 if i==0 else .9,last=2 if i==0 else .9)
        w['metrics'].update(load1=load, memory_available_ratio=memory)
        rows.append(w)
    source=artifacts(tmp_path/'input',rows)
    s=discover_states(source,'stage2',tmp_path/'out',candidate_policy='conservative')
    assert s['candidate_events']==0

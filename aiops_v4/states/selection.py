"""Conservative queue admission; rejected signals remain queryable observations."""
from .reference import normalize
import math


def _mean(row, name):
    metric = row['rule_inputs'].get(name, {})
    return metric.get('mean') if metric.get('usable') else None


def _resource_signals(row, reference, initial_memory=None):
    if row['source'] != 'node' or row['quality']['blocked']:
        return []
    signals=[]
    for name,bound,direction in (
        ('cpu_usage',80,'high'), ('disk_io_util',80,'high'),
        ('memory_available_ratio',.15,'low'), ('swap_used_ratio',.5,'high'),
        ('filesystem_used_ratio',.9,'high'), ('inode_used_ratio',.9,'high'), ('open_fd_ratio',.8,'high')):
        value=_mean(row,name)
        upper=100 if name in {'cpu_usage','disk_io_util'} else 1
        if value is not None and 0<=value<=upper and (value>=bound if direction=='high' else value<=bound):
            signals.append('resource_pressure_observation:'+name)
    available=_mean(row,'memory_available_ratio')
    if initial_memory is not None:
        initial_available=initial_memory.get('memory_available_ratio')
        if initial_available is not None and available is not None and 0<=available<=1 and available<=initial_available-.05:
            signals.append('resource_pressure_observation:memory_available_ratio')
        cpu,load=_mean(row,'cpu_usage'),_mean(row,'load1')
        initial_cpu,initial_load=initial_memory.get('cpu_usage'),initial_memory.get('load1')
        if all(v is not None for v in (cpu,load,initial_cpu,initial_load)) and 0<=cpu<=100 and cpu>=initial_cpu+10 and load>=initial_load+.5:
            signals.extend(('resource_pressure_observation:cpu_usage','resource_pressure_observation:load1'))
    count=_mean(row,'process_count');prior=reference['features'].get('process_count::mean')
    if count is not None and prior is not None:
        center=math.expm1(prior['median']) if prior['transform']=='log1p' and prior['median']<700 else prior['median']
        if count>=max(center*3,center+100):signals.append('resource_pressure_observation:process_count')
    return sorted(set(signals))


def _mechanism(row, reference):
    """Admission hypotheses, not fault labels; inferred units remain explicit."""
    if row['source'] == 'node':
        return bool(_resource_signals(row,reference))
    if row['source'] == 'interface':
        for direction in ('rx','tx'):
            bad = sum(_mean(row,direction+'_'+kind+'_rate') or 0 for kind in ('drop','error'))
            packets = _mean(row,direction+'_packets_rate')
            if bad >= 1 or (packets is not None and packets > 0 and bad/packets >= .01):
                return True
        return False
    if row['source'] == 'traffic':
        for name, m in row['rule_inputs'].items():
            if not m['usable']: continue
            mean = m.get('mean')
            if ('latency_' in name and name.endswith('_seconds') and mean is not None and mean >= .5) or (
                name.endswith('_loss_rate') and mean is not None and mean >= .01):
                return True
            if m['kind']=='counter' and not m.get('counter_decrease_count') and not m.get('counter_gap_count') and (
                any(word in name for word in ('failed_total','error_total','timeout_total','retransmits_total')) and (m.get('delta_sum') or 0)>=3):
                return True
        return False
    if row['source'] == 'routing':
        return any(m['usable'] and (name == 'ospf6_interface_cost' or (name == 'ospf6_neighbor_state_code' and m.get('last') != 8))
                   for name,m in row['rule_inputs'].items())
    return False


def eligibility(row, state, reference, threshold):
    if not state['candidate']:
        return False, False, None
    if row['source'] in {'netflow', 'frr'}:
        return False, False, 'context_only_source'
    if row['quality']['blocked']:
        return False, False, 'blocked_quality'
    rules = state['rule_signals'] if any(t.startswith('rule:') for t in state['triggers']) else []
    direct=[r.split(':',1)[1] for r in rules if r.startswith('protocol_indicator_zero:')]
    if direct:
        # A configured but persistently absent session/route is not an observed
        # interruption. Insufficient references retain the explicit zero cue.
        if any('1' in reference['features'].get(name+'::last',{}).get('categories',{'1':1}) for name in direct):
            return True, True, None
        return False, False, 'static_protocol_zero'
    if any(r.startswith('protocol_state_nonfull:') for r in rules):
        if '8' in reference['features'].get('ospf6_neighbor_state_code::last',{}).get('categories',{'8':1}):
            return True, True, None
        return False, False, 'static_protocol_state'
    if any(r.startswith('resource_pressure_observation:') for r in rules):
        return True, False, None
    if any(r.startswith('collection_unavailable:') for r in rules):
        return True, False, None
    # Do not use rare support or categorical novelty as a fault label.
    robust = 'statistics:robust_deviation' in state['triggers']
    normalized = normalize(row, reference)
    material = False
    for name, deviation in normalized['deviations'].items():
        if deviation < threshold:
            continue
        cell, prior = row['features'][name], reference['features'][name]
        floor = (5.0 if cell['unit'] == 'percent' else .05 if cell['unit'] in {'ratio', 'fraction', 'seconds'}
                 else 1.0 if cell['unit'] in {'milliseconds', 'ms'} else .5)
        # Log-transformed quantity requires a meaningful multiplicative change.
        if cell['transform'] == 'log1p': floor = .5
        if abs(cell['value'] - prior['median']) >= max(floor, abs(prior['median']) * .1):
            material = True
            break
    if robust and material:
        if _mechanism(row,reference):
            return True, False, None
        return False, False, 'no_failure_mechanism'
    if any(r.startswith('business_failure_activity:') and (row['rule_inputs'][r.split(':', 1)[1]].get('delta_sum') or 0) >= 3 for r in rules):
        return True, False, None
    if any(r.startswith('interface_drop_or_error_nonzero:') and (row['rule_inputs'][r.split(':', 1)[1]].get('mean') or 0) >= 1 for r in rules):
        return True, False, None
    return False, False, 'small_effect' if robust else 'uncorroborated_signal'


def select_candidates(rows, reference, *, threshold, minimum_windows, rules_enabled=True):
    """At most minimum_windows buffered; preserve the first qualifying window.

    A direct protocol interruption admits its contiguous run immediately. Weak
    signals need consecutive windows. A normal row or missing interval closes
    the run; it is never bridged or assigned to a fault.
    """
    pending, admitted, prior_end, initial_memory = [], False, None, None
    def suppress():
        for row, state in pending:
            state.update(candidate=False, suppression_reason='insufficient_persistence')
            yield row, state
        pending.clear()
    for row, state in rows:
        if rules_enabled:
            if initial_memory is None and row['source']=='node' and not row['quality']['blocked']:
                values={name:_mean(row,name) for name in ('memory_available_ratio','cpu_usage','load1')}
                for name,value in values.items():
                    upper={'memory_available_ratio':1,'cpu_usage':100}.get(name,math.inf)
                    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=upper:
                        values[name]=None
                if any(v is not None for v in values.values()):
                    initial_memory=dict(values,vector_id=row['vector_id'],start_time=row['start_time'],
                                        interpretation='initial_observation_not_known_healthy')
            pressure=_resource_signals(row,reference,initial_memory)
            metric=row['rule_inputs'].get('ospf6_neighbor_state_code',{})
            code=metric.get('last')
            minimum=metric.get('min')
            if type(minimum) in (int,float) and 0<=minimum<8 and int(minimum)==minimum:
                code=minimum
            if (row['source']=='routing' and not row['quality']['blocked'] and metric.get('usable')
                    and type(code) in (int,float) and 0<=code<8 and int(code)==code):
                pressure.append('protocol_state_nonfull:ospf6_neighbor_state_code')
            if pressure:
                state['rule_signals']=sorted(set(state['rule_signals']+pressure))
                state['triggers']=sorted(set(state['triggers']+['rule:'+r for r in pressure]))
                state['candidate']=True
                if initial_memory is not None:
                    state['admission_reference']=dict(initial_memory)
        state['raw_candidate'] = state['candidate']
        keep, direct, reason = eligibility(row, state, reference, threshold)
        if not keep or (prior_end is not None and prior_end != row['start_time']):
            yield from suppress()
            admitted = False
        prior_end = row['end_time']
        if not keep:
            state.update(candidate=False, suppression_reason=reason)
            yield row, state
            continue
        state['suppression_reason'] = None
        if admitted:
            yield row, state
        else:
            pending.append((row, state))
            if direct or len(pending) >= minimum_windows:
                yield from pending
                pending.clear()
                admitted = True
    yield from suppress()

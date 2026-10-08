"""Observation rules and state changes carry no root-cause or fault labels."""
from .reference import normalize
from .cluster import assign


def _positive(value):
    return value is not None and value > 0


def rule_signals(row, previous=None):
    rules, quality = [], []
    adjacent = previous is not None and previous['end_time'] == row['start_time']
    for name, metric in row['rule_inputs'].items():
        for field, label in [('counter_decrease_count', 'counter_decrease'), ('counter_gap_count', 'counter_gap')]:
            if _positive(metric.get(field)):
                quality.append(label + ':' + name)
        if not metric['usable']:
            continue
        last = metric.get('last')
        if row['source'] == 'scrape' and name == 'scrape_up' and last == 0:
            rules.append('collection_unavailable:' + name)
        if row['source'] == 'routing' and name in {'bgp_peer_up', 'ipv6_route_exists'} and last == 0:
            rules.append('protocol_indicator_zero:' + name)
        if row['source'] == 'interface' and ('drop_rate' in name or 'error_rate' in name) and _positive(metric.get('mean')):
            rules.append('interface_drop_or_error_nonzero:' + name)
        valid_counter = metric['kind'] == 'counter' and not metric.get('counter_decrease_count') and not metric.get('counter_gap_count')
        if valid_counter and _positive(metric.get('delta_sum')):
            if row['source'] == 'traffic' and any(word in name for word in ('failed_total', 'error_total', 'timeout_total')):
                rules.append('business_failure_activity:' + name)
            if row['source'] == 'routing' and 'route' in name and 'change' in name:
                rules.append('route_change_activity:' + name)
        if adjacent and metric['unit'] == 'state_code':
            old = previous['rule_inputs'].get(name, {})
            if old.get('usable') and old.get('last') is not None and last is not None and old['last'] != last:
                rules.append('observed_state_change:' + name)
        if metric['unit'] == 'state_code' and _positive(metric.get('change_count')):
            rules.append('observed_state_change:' + name)
    if row['source'] == 'frr' and not row['quality']['blocked'] and _positive(row.get('event_count')):
        severity = str(row['dimensions'].get('severity', '')).lower()
        if severity in {'emerg', 'alert', 'crit', 'err', 'error', 'warning', 'warn'}:
            rules.append('log_severity_present:' + severity)
    return sorted(set(rules)), sorted(set(quality))


def assess(row, reference, clusters, mode, thresholds, previous=None) -> dict:
    normalized = normalize(row, reference)
    assigned = assign(normalized['values'], clusters, thresholds['minimum_overlap'])
    scorable = reference['status'] == 'ready' and normalized['coverage'] >= thresholds['minimum_overlap'] and not row['quality']['blocked']
    status = 'blocked_quality' if row['quality']['blocked'] else 'insufficient_reference' if reference['status'] != 'ready' else 'scored' if scorable else 'insufficient_feature_overlap'
    old_row, old_state = previous if previous is not None else (None, None)
    rules, quality = rule_signals(row, old_row)
    statistical, cluster_signals = [], []
    stat_score = normalized['stat_score'] if scorable else None
    if stat_score is not None and stat_score >= thresholds['stat_threshold']:
        statistical.append('statistics:robust_deviation')
    if scorable and normalized['unseen_categories']:
        statistical.append('statistics:unseen_category')
    if scorable and assigned['distance'] is not None:
        if assigned['distance'] >= thresholds['cluster_threshold']:
            cluster_signals.append('cluster:distant_state')
        if assigned['support'] <= thresholds['rare_fraction']:
            cluster_signals.append('cluster:rare_reference_state')
    triggers = (statistical if mode in {'statistics', 'hybrid'} else []) + (cluster_signals if mode in {'cluster', 'hybrid'} else [])
    if thresholds['rules_enabled']:
        triggers += ['rule:' + rule for rule in rules]
    transition = bool(old_state is not None and old_row['end_time'] == row['start_time'] and
                      old_state['cluster_id'] is not None and assigned['cluster_id'] is not None and
                      old_state['cluster_id'] != assigned['cluster_id'])
    drivers = [{'feature': name, 'deviation': deviation, 'raw_value': row['features'][name]['raw_value'],
                'unit': row['features'][name]['unit'], 'transform': row['features'][name]['transform'],
                'semantic_status': row['features'][name]['semantic_status']}
               for name, deviation in sorted(normalized['deviations'].items(), key=lambda item: (-item[1], item[0]))[:5]]
    return {key: row[key] for key in ('vector_id', 'group_id', 'batch', 'source', 'view', 'identity', 'dimensions',
                                     'start_time', 'end_time', 'references', 'references_truncated')} | {
        'schema_version': 1, 'reference_status': reference['status'], 'scoring_status': status,
        'is_reference': row.get('in_reference', False), 'feature_coverage': normalized['coverage'],
        'time_coverage': row['quality']['time_coverage'], 'stat_score': stat_score,
        'cluster_id': assigned['cluster_id'], 'cluster_distance': assigned['distance'],
        'cluster_reference_support': assigned['support'], 'cluster_support_basis': 'bounded_reference_sample',
        'state_changed': transition, 'unseen_categories': normalized['unseen_categories'],
        'quality_signals': quality, 'quality_flags': row['quality']['flags'],
        'overflowed_features': normalized['overflowed_features'], 'drivers': drivers,
        'statistical_signals': statistical, 'cluster_signals': cluster_signals, 'rule_signals': rules,
        'triggers': sorted(set(triggers)), 'candidate': bool(triggers)}

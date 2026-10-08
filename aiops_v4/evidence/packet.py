"""Compact packets preserve native grains, missingness and retrieval anchors."""


def compact_observation(state, matrix):
    fields = ('vector_id', 'group_id', 'batch', 'source', 'view', 'identity', 'dimensions',
              'start_time', 'end_time', 'candidate', 'scoring_status', 'reference_status',
              'is_reference', 'feature_coverage', 'time_coverage', 'stat_score',
              'cluster_id', 'cluster_distance', 'cluster_reference_support', 'cluster_support_basis', 'state_changed')
    result = {k: state[k] for k in fields if k in state}
    result['drivers'] = state.get('drivers', [])[:5]
    selected = {d.get('feature') for d in result['drivers']}
    features = matrix.get('features', {})
    result['feature_values'] = {k: v for k, v in features.items() if k in selected}
    # Binary facts (including recovered last values) remain visible to role agents.
    rule_inputs = matrix.get('rule_inputs', {})
    proof_metrics = {s.split(':', 1)[1] for s in state.get('rule_signals', []) if ':' in s}
    selected_rules = [(k, v) for k, v in rule_inputs.items() if v.get('unit') in {'binary', 'state_code'} or k in proof_metrics or k == 'up']
    result['rule_inputs'] = dict(selected_rules[:16])
    result['references'] = state.get('references', [])[:4]
    result['references_truncated'] = state.get('references_truncated', False) or len(state.get('references', [])) > 4
    for field in ('statistical_signals', 'cluster_signals', 'rule_signals', 'quality_signals', 'quality_flags', 'triggers', 'unseen_categories'):
        result[field] = state.get(field, [])[:8]
    result['details_truncated'] = len(selected_rules) > 16 or any(len(state.get(f, [])) > 8 for f in
        ('statistical_signals', 'cluster_signals', 'rule_signals', 'quality_signals', 'quality_flags', 'triggers', 'unseen_categories'))
    return result


def make_packet(bundle, support, counter, quality, source_metadata, relations, limits):
    cap = limits.get('observations_per_kind', 8)
    if type(cap) is not int or not 1 <= cap <= 32:
        raise ValueError('observations_per_kind must be 1..32')
    support = [o for o in support if o.get('candidate') is True]
    counter = [o for o in counter if o.get('candidate') is False and o.get('scoring_status') == 'scored']
    lists = {'support': support, 'observations_without_current_trigger': counter, 'quality_context': quality}
    result = dict(bundle)
    totals = limits.get('totals', {})
    result['observation_counts'] = {key: totals.get(key, len(value)) for key, value in lists.items()}
    result['truncation'] = {key: result['observation_counts'][key] > cap for key in lists}
    result.update({key: value[:cap] for key, value in lists.items()})
    result['counterevidence_interpretation'] = 'scorable_without_current_trigger; not_known_normal'
    result['context'] = source_metadata
    result['relations'] = relations[:8]
    result['relation_count'] = totals.get('relations', len(relations))
    result['truncation']['relations'] = result['relation_count'] > 8
    refs, ids = [], set()
    truncated = bundle.get('references_truncated', False)
    for obs in [bundle, *result['support'], *result['observations_without_current_trigger'], *result['quality_context']]:
        truncated |= obs.get('references_truncated', False)
        for ref in obs.get('references', []):
            if ref['record_id'] in ids:
                continue
            if len(refs) < 32:
                refs.append(ref)
                ids.add(ref['record_id'])
            else:
                truncated = True
    result['references'], result['references_truncated'] = refs, truncated
    result['truncation']['references'] = truncated
    limitations = ['semantic_units_not_fully_verified', 'association_not_confirmed_fault', 'bundle_envelope_not_fault_duration']
    for source, info in sorted(source_metadata.get('sources', {}).items()):
        if not info.get('files') or not info.get('rows'):
            limitations.append('source_unavailable:' + source)
    scope = source_metadata.get('scope', {})
    if scope.get('mode') == 'prefix_sample':
        limitations.append('prefix_sample')
    if not scope.get('timezone_officially_confirmed', False):
        limitations.append('timezone_unverified')
    if not source_metadata.get('topology_available', False):
        limitations.append('topology_unavailable')
    if not source_metadata.get('raw_materialized', False):
        limitations.append('raw_not_materialized')
    if truncated:
        limitations.append('references_truncated; query_states_for_additional_anchors')
    for source, info in sorted(source_metadata.get('local_availability', {}).items()):
        if not info['observed']:
            limitations.append('locally_unobserved:' + source)
        elif not info['scorable']:
            limitations.append('locally_unscorable:' + source)
    result['limitations'] = limitations
    return result

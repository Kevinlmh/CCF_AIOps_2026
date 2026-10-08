"""Select interpretable features without joining different observation grains."""
import hashlib
import json
import math
from aiops_v4.data.reader import utc_time

MAX_FEATURES = 128
LOG_UNITS = {'count', 'load', 'bytes', 'packets', 'samples', 'exported_records', 'prefixes', 'cost'}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def window_vector(window: dict, batch: str) -> dict:
    if window.get('batch') != batch:
        raise ValueError('mixed batches are not allowed')
    if not isinstance(window.get('window_id'), str) or not window['window_id']:
        raise ValueError('window_id must be non-empty')
    start = utc_time(window['start_time'], require_timezone=True)[0]
    end = utc_time(window['end_time'], require_timezone=True)[0]
    if end <= start:
        raise ValueError('window end must follow start')
    identity, dimensions = window['identity'], window['dimensions']
    if not identity.get('entity_id') or not isinstance(dimensions, dict):
        raise ValueError('window identity and dimensions are required')
    grain = [batch, window['source'], window['view'], identity.get('role'), identity['entity_id'], dimensions]
    group_key = json.dumps(grain, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    flags = window.get('quality_flags', [])
    blocked = any(flag in {'city_conflict', 'city_entity_conflict'} or
                  flag.startswith('missing_dimension:') for flag in flags)
    features, excluded, rule_inputs, notes = {}, [], {}, []
    for name, metric in sorted(window['metrics'].items()):
        semantic = metric['semantics']
        kind, unit = semantic['kind'], semantic.get('unit')
        if kind not in {'gauge', 'counter', 'interval_count'}:
            excluded.append(name)
            continue
        category = unit in {'binary', 'state_code'}
        stat = 'last' if category else {'gauge': 'mean', 'counter': 'rate_mean', 'interval_count': 'sum'}[kind]
        raw = metric.get(stat)
        transform = 'category' if category else 'log1p' if unit in LOG_UNITS or (unit and unit.endswith('/second')) else 'identity'
        bad = metric.get('invalid_input_count', 0) or metric.get('conflicting_time_count', 0)
        broken = kind == 'counter' and (metric.get('counter_decrease_count', 0) or metric.get('counter_gap_count', 0))
        value = None
        if not blocked and not bad and not broken and _finite(raw):
            if category:
                value = format(raw, '.17g')
            elif transform == 'log1p':
                if raw >= 0:
                    value = math.log1p(raw)
            else:
                value = float(raw)
        if value is None:
            notes.append(f'masked:{name}')
        features[name + '::' + stat] = {'value': value, 'raw_value': raw if _finite(raw) else None,
                                      'kind': 'category' if category else 'numeric', 'metric': name,
                                      'stat': stat, 'transform': transform, 'unit': unit,
                                      'semantic_status': semantic.get('status', 'unverified')}
        rule_inputs[name] = {key: metric.get(key) for key in ('last', 'mean', 'delta_sum', 'rate_mean',
                            'change_count', 'counter_decrease_count', 'counter_gap_count')}
        rule_inputs[name].update(kind=kind, unit=unit, usable=not blocked and not bad,
                                 semantic_status=semantic.get('status', 'unverified'))
    if len(features) > MAX_FEATURES:
        raise ValueError('feature limit exceeded; refine the observation schema')
    return {'schema_version': 1, 'vector_id': window['window_id'], 'group_id': hashlib.sha256(group_key.encode()).hexdigest(),
            'batch': batch, 'source': window['source'], 'view': window['view'], 'identity': identity,
            'dimensions': dimensions, 'start_time': start, 'end_time': end,
            'features': features, 'excluded_metrics': excluded, 'rule_inputs': rule_inputs,
            'quality': {'blocked': blocked, 'flags': flags, 'masked': notes,
                        'time_coverage': window.get('coverage', {}).get('fraction')},
            'references': window.get('references', []),
            'references_truncated': window.get('references_truncated', False),
            'event_count': window.get('event_count')}

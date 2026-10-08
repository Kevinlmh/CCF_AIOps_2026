"""Bounded robust references describe observations, not known healthy states."""
from collections import Counter
import math
import random
from .matrix import MAX_FEATURES


class Reservoir:
    def __init__(self, capacity):
        self.capacity, self.count, self.items = capacity, 0, []
        self.random = random.Random(0)

    def add(self, value):
        self.count += 1
        if len(self.items) < self.capacity:
            self.items.append(value)
        else:
            index = self.random.randrange(self.count)
            if index < self.capacity:
                self.items[index] = value


def _quantile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    weight = position - lo
    return values[lo] * (1 - weight) + values[hi] * weight


def fit_reference(rows, min_reference=12, capacity=256) -> dict:
    if type(min_reference) is not int or min_reference < 2 or type(capacity) is not int or capacity < min_reference:
        raise ValueError('reference capacity must cover at least two minimum observations')
    if capacity > 256:
        raise ValueError('reference sample capacity must not exceed 256')
    columns, pool = {}, Reservoir(64)
    group_id, observed, eligible = None, 0, 0
    for row in rows:
        if group_id is not None and row['group_id'] != group_id:
            raise ValueError('reference rows must belong to one group')
        group_id = row['group_id']
        observed += 1
        if row['quality']['blocked']:
            continue
        if any(cell['value'] is not None for cell in row['features'].values()):
            eligible += 1
            pool.add(row)
        for name, cell in row['features'].items():
            descriptor = {key: value for key, value in cell.items() if key not in {'value', 'raw_value'}}
            if name not in columns:
                if len(columns) >= MAX_FEATURES:
                    raise ValueError('reference feature limit exceeded')
                columns[name] = {'descriptor': descriptor, 'sample': Reservoir(capacity),
                                 'categories': Counter(), 'category_overflow': False}
            column = columns[name]
            if descriptor != column['descriptor']:
                raise ValueError('inconsistent feature semantics within group')
            value = cell['value']
            if value is None:
                continue
            column['sample'].add(value)
            if cell['kind'] == 'category' and not column['category_overflow']:
                column['categories'][value] += 1
                if len(column['categories']) > 16:
                    column['category_overflow'] = True
                    column['categories'].clear()
    features, excluded = {}, {}
    for name, column in sorted(columns.items()):
        sample, descriptor = column['sample'], column['descriptor']
        if sample.count < min_reference:
            excluded[name] = 'insufficient_observations'
            continue
        feature = {**descriptor, 'observations': sample.count, 'sample_count': len(sample.items),
                   'quantiles_exact': sample.count <= capacity}
        if descriptor['kind'] == 'category':
            if column['category_overflow']:
                excluded[name] = 'category_limit_exceeded'
                continue
            feature['categories'] = dict(sorted(column['categories'].items()))
        else:
            center = _quantile(sample.items, 0.5)
            deviations = [abs(value - center) for value in sample.items]
            if not all(math.isfinite(value) for value in deviations):
                excluded[name] = 'nonfinite_reference_scale'
                continue
            scale, basis = _quantile(deviations, 0.5), 'mad'
            if scale == 0:
                scale, basis = (_quantile(sample.items, 0.75) - _quantile(sample.items, 0.25)) / 2, 'iqr_half'
            if scale == 0:
                scale, basis = max(abs(center) * 0.01, 1e-6), 'constant_floor'
            if not math.isfinite(scale):
                excluded[name] = 'nonfinite_reference_scale'
                continue
            feature.update(median=center, scale=scale, scale_basis=basis)
        features[name] = feature
    return {'status': 'ready' if features else 'insufficient_reference', 'group_id': group_id,
            'observed_reference_rows': observed, 'eligible_reference_rows': eligible,
            'min_reference': min_reference, 'capacity': capacity, 'features': features,
            'excluded_features': excluded, '_sample_rows': pool.items}


def normalize(row: dict, reference: dict) -> dict:
    if reference['group_id'] not in {None, row['group_id']}:
        raise ValueError('reference group does not match vector')
    by_metric = {feature['metric']: feature for feature in reference['features'].values()}
    for cell in row['features'].values():
        prior = by_metric.get(cell['metric'])
        if prior is not None:
            descriptor = {key: value for key, value in cell.items() if key not in {'value', 'raw_value'}}
            if any(prior.get(key) != value for key, value in descriptor.items()):
                raise ValueError('assessment semantics do not match reference')
    values, deviations, unseen, overflow = {}, {}, [], []
    for name, feature in reference['features'].items():
        cell = row['features'].get(name, {})
        value = None if row['quality']['blocked'] else cell.get('value')
        if value is not None and feature['kind'] == 'numeric':
            value = (value - feature['median']) / feature['scale']
            if not math.isfinite(value):
                value = None
                overflow.append(name)
            else:
                deviations[name] = abs(value)
        elif value is not None and value not in feature['categories']:
            unseen.append(name)
        values[name] = value
    return {'values': values, 'coverage': sum(value is not None for value in values.values()) / len(values) if values else 0,
            'stat_score': max(deviations.values()) if deviations else None,
            'deviations': deviations, 'unseen_categories': unseen, 'overflowed_features': overflow}

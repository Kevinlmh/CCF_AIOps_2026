"""Bounded aggregation over disk-sorted series, with explicit counter breaks."""

from datetime import datetime, timedelta
import hashlib
from itertools import groupby
import math

from aiops_v4.data.reader import is_missing
from aiops_v4.data.statistics import ColumnStats
from .semantics import metric_semantics
from .time import iso_time, window_start

REFERENCE_LIMIT = 8
RESERVOIR_SIZE = 128
METRIC_LIMIT = 2048


class _Metric:
    def __init__(self, source, name):
        self.semantics = metric_semantics(source, name)
        self.samples = ColumnStats(True, RESERVOIR_SIZE, 128)
        self.rates = ColumnStats(True, RESERVOIR_SIZE, 128)
        self.inputs = self.missing = self.invalid = self.finite = self.duplicates = self.conflicts = 0
        self.decreases = self.gaps = self.first_counter = self.changes = 0
        self.first = self.last = self.first_time = None
        self.total = self.delta_total = 0.0
        self.x_mean = self.y_mean = self.x_m2 = self.xy = 0.0

    def input(self, raw):
        self.inputs += 1
        if is_missing(raw):
            self.missing += 1
            return None
        try:
            value = float(raw)
        except (ValueError, TypeError):
            self.invalid += 1
            return None
        if not math.isfinite(value) or (self.semantics['kind'] in {'counter', 'interval_count'} and value < 0):
            self.invalid += 1
            return None
        self.finite += 1
        return value

    def add(self, value, timestamp):
        self.samples.add(str(value))
        if self.first_time is None:
            self.first_time, self.first = timestamp, value
        if self.last is not None and value != self.last:
            self.changes += 1
        self.last = value
        self.total += value
        x = (timestamp - self.first_time).total_seconds()
        dx, dy = x - self.x_mean, value - self.y_mean
        self.x_mean += dx / self.samples.n
        self.y_mean += dy / self.samples.n
        self.x_m2 += dx * (x - self.x_mean)
        self.xy += dx * (value - self.y_mean)

    def report(self):
        raw = self.samples.report()
        rates = self.rates.report()
        result = {**raw, 'semantics': self.semantics, 'input_count': self.inputs,
                  'missing_input_count': self.missing, 'invalid_input_count': self.invalid,
                  'finite_input_count': self.finite, 'used_sample_count': self.samples.n,
                  'missing_fraction': self.missing / self.inputs if self.inputs else None,
                  'duplicate_input_count': self.duplicates, 'conflicting_time_count': self.conflicts,
                  'first': self.first, 'last': self.last, 'change_count': self.changes,
                  'slope_per_second': self.xy / self.x_m2 if self.x_m2 > 0 and self.semantics['kind'] == 'gauge' else None,
                  'sum': self.total if self.samples.n and self.semantics['kind'] == 'interval_count' else None,
                  'delta_sum': self.delta_total if self.rates.n else None,
                  'rate_mean': rates['mean'], 'rate_min': rates['min'], 'rate_max': rates['max'],
                  'rate_sample_count': self.rates.n,
                  'rate_unit': f"{self.semantics['unit']}/second" if self.semantics['kind'] == 'counter' else None,
                  'counter_decrease_count': self.decreases, 'counter_gap_count': self.gaps,
                  'counter_first_sample_count': self.first_counter}
        overflow = list(result['overflowed_statistics'])
        for name, value in result.items():
            if isinstance(value, float) and not math.isfinite(value):
                result[name] = None
                overflow.append(name)
        result['overflowed_statistics'] = sorted(set(overflow))
        return result


class _Window:
    def __init__(self, row, start, width, expected_step):
        self.template = row
        self.start, self.width, self.expected_step = start, width, expected_step
        self.metrics = {}
        self.record_count = self.timestamps = 0
        self.flags = set()
        self.references = []
        self.reference_ids = set()
        self.truncated = False

    def reference(self, ref):
        if ref['record_id'] in self.reference_ids:
            return
        if len(self.references) < REFERENCE_LIMIT:
            self.references.append(ref)
            self.reference_ids.add(ref['record_id'])
        else:
            self.truncated = True

    def metric(self, name):
        if name not in self.metrics:
            if len(self.metrics) >= METRIC_LIMIT:
                raise ValueError('window metric limit exceeded; refine the observation schema')
            self.metrics[name] = _Metric(self.template['source'], name)
        return self.metrics[name]

    def report(self):
        expected = self.width // self.expected_step if self.expected_step else None
        if expected and self.timestamps > expected:
            self.flags.add('denser_than_expected_cadence')
        key = self.template['series_key'] + '|' + iso_time(self.start) + '|' + str(self.width)
        return {'schema_version': 1, 'window_id': hashlib.sha256(key.encode()).hexdigest(),
                'batch': self.template['batch'], 'source': self.template['source'], 'view': self.template['view'],
                'identity': self.template['identity'], 'dimensions': self.template['dimensions'],
                'start_time': iso_time(self.start), 'end_time': iso_time(self.start + timedelta(seconds=self.width)),
                'record_count': self.record_count, 'event_count': self.record_count if self.template['source'] == 'frr' else None,
                'coverage': {'observed_timestamps': self.timestamps, 'expected_step_seconds': self.expected_step,
                             'expected_points': expected, 'fraction': min(1.0, self.timestamps / expected) if expected else None,
                             'basis': 'explicit_cadence_assumption' if expected else 'observed_only'},
                'metrics': {name: metric.report() for name, metric in sorted(self.metrics.items())},
                'quality_flags': sorted(self.flags), 'references': self.references,
                'reference_limit': REFERENCE_LIMIT, 'references_truncated': self.truncated}


def iter_windows(ordered_records, batch: str, window_seconds: int = 60,
                 expected_step_seconds: int | None = None, counter_max_gap_seconds: int = 180):
    """Input must be sorted by series_key then UTC timestamp; no empty windows emitted."""
    for name, value in [('window_seconds', window_seconds), ('counter_max_gap_seconds', counter_max_gap_seconds)]:
        if type(value) is not int or value <= 0:
            raise ValueError(f'{name} must be positive')
    if expected_step_seconds is not None and (type(expected_step_seconds) is not int or
            expected_step_seconds <= 0 or window_seconds % expected_step_seconds):
        raise ValueError('expected_step_seconds must be a positive divisor of window_seconds')
    last_key = None
    for series_key, series_rows in groupby(ordered_records, key=lambda row: row['series_key']):
        if last_key is not None and series_key <= last_key:
            raise ValueError('records must be sorted by series_key')
        last_key = series_key
        previous = {}
        current = None
        last_time = None
        for time_string, same_time in groupby(series_rows, key=lambda row: row['timestamp']):
            timestamp = datetime.fromisoformat(time_string.replace('Z', '+00:00'))
            if last_time is not None and timestamp <= last_time:
                raise ValueError('series timestamps must be sorted')
            last_time = timestamp
            bucket = window_start(timestamp, window_seconds)
            frame = {}
            for row in same_time:
                if row['batch'] != batch:
                    raise ValueError('mixed batches are not allowed')
                if current is None or current.start != bucket:
                    if current is not None:
                        yield current.report()
                    current = _Window(row, bucket, window_seconds, expected_step_seconds)
                current.record_count += 1
                current.flags.update(row['flags'])
                current.reference(row['ref'])
                for name, raw in row['raw_values'].items():
                    metric = current.metric(name)
                    value = metric.input(raw)
                    if metric.semantics['kind'] == 'interval_count':
                        if value is not None:
                            metric.add(value, timestamp)
                        continue
                    point = frame.setdefault(name, {'value': None, 'valid': 0, 'conflict': False, 'bad': False, 'ref': row['ref']})
                    if value is None:
                        point['bad'] = True
                        continue
                    if point['valid'] and value != point['value']:
                        point['conflict'] = True
                    if not point['valid']:
                        point['value'], point['ref'] = value, row['ref']
                    point['valid'] += 1
            current.timestamps += 1
            for name in previous.keys() - frame.keys():
                previous[name] = None
                current.flags.add(f'counter_field_absent:{name}')
            for name, point in frame.items():
                metric = current.metric(name)
                counter = metric.semantics['kind'] == 'counter'
                if point['conflict']:
                    metric.conflicts += 1
                    current.flags.add(f'conflicting_sample:{name}')
                elif point['valid'] > 1:
                    metric.duplicates += point['valid'] - 1
                    current.flags.add(f'duplicate_sample:{name}')
                usable = point['valid'] and not point['conflict'] and not (counter and point['bad'])
                if not usable:
                    if counter:
                        previous[name] = None
                    continue
                value = point['value']
                metric.add(value, timestamp)
                if not counter:
                    continue
                prior = previous.get(name)
                if prior is None:
                    metric.first_counter += 1
                else:
                    old_time, old_value, old_ref = prior
                    gap = (timestamp - old_time).total_seconds()
                    delta = value - old_value
                    current.reference(old_ref)
                    if value < old_value:
                        metric.decreases += 1
                        current.flags.add(f'counter_decrease:{name}')
                    elif gap > counter_max_gap_seconds:
                        metric.gaps += 1
                        current.flags.add(f'counter_gap:{name}')
                    elif not math.isfinite(delta):
                        current.flags.add(f'counter_overflow:{name}')
                    else:
                        metric.rates.add(str(delta / gap))
                        metric.delta_total += delta
                previous[name] = (timestamp, value, point['ref'])
        if current is not None:
            yield current.report()

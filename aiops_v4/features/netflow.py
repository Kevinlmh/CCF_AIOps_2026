"""Bounded disk-backed interval totals, without staging individual flows.

NetFlow state features consume sums. Counts, moments, invalid/missing inputs and
raw anchors are retained; per-flow quantiles and change counts are deliberately
not computed in this mode. Unordered and cross-file rows share the same bucket.
"""
from collections import OrderedDict
from datetime import datetime
import hashlib
import json
import math
from aiops_v4.data.reader import is_missing
from aiops_v4.storage import db_dump, load_json
from .aggregate import _Window, _Metric, REFERENCE_LIMIT
from .time import iso_time, window_start


class NetflowAccumulator:
    def __init__(self, db, width, step, capacity=2048):
        self.db, self.width, self.step, self.capacity = db, width, step, capacity
        self.cache = OrderedDict()
        db.execute('CREATE TABLE flow_windows(series TEXT,start TEXT,payload BLOB,PRIMARY KEY(series,start)) WITHOUT ROWID')
        db.execute('CREATE TABLE flow_times(series TEXT,start TEXT,time TEXT,PRIMARY KEY(series,start,time)) WITHOUT ROWID')

    def _save(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO flow_windows VALUES (?,?,?)', (*key, db_dump(value)))

    def add(self, row):
        if row['source'] != 'netflow':
            raise ValueError('NetFlow accumulator source mismatch')
        stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
        start = iso_time(window_start(stamp, self.width))
        key = row['series_key'], start
        value = self.cache.pop(key, None)
        if value is None:
            saved = self.db.execute('SELECT payload FROM flow_windows WHERE series=? AND start=?', key).fetchone()
            value = load_json(saved[0]) if saved else dict(template=row, count=0, flags=[], refs=[], metrics={})
        value['count'] += 1
        value['flags'] = sorted(set(value['flags']) | set(row['flags']))
        ref = dict(time=row['timestamp'], ref=row['ref'])
        # Source files are read in deterministic order; ties use their provenance.
        value['refs'] = sorted(value['refs'] + [ref], key=lambda r: (r['time'], r['ref']['source_file'], r['ref']['record_index']))[:REFERENCE_LIMIT]
        if value.get('last_time_seen') != row['timestamp']:
            self.db.execute('INSERT OR IGNORE INTO flow_times VALUES (?,?,?)', (*key, row['timestamp']))
            value['last_time_seen'] = row['timestamp']
        for name, raw in row['raw_values'].items():
            m = value['metrics'].setdefault(name, dict(inputs=0, missing=0, invalid=0, n=0, zeros=0,
                           mean=0., m2=0., total=0., min=None, max=None, first=None, last=None))
            m['inputs'] += 1
            if is_missing(raw):
                m['missing'] += 1
                continue
            try:
                number = float(raw)
            except (ValueError, TypeError):
                number = float('nan')
            if not math.isfinite(number) or number < 0:
                m['invalid'] += 1
                continue
            m['n'] += 1
            m['zeros'] += number == 0
            delta = number - m['mean']
            m['mean'] += delta / m['n']
            if m['m2'] is not None:
                m['m2'] += delta * (number - m['mean'])
                if not math.isfinite(m['m2']): m['m2'] = None
            if m['total'] is not None:
                m['total'] += number
                if not math.isfinite(m['total']): m['total'] = None
            m['min'] = number if m['min'] is None else min(m['min'], number)
            m['max'] = number if m['max'] is None else max(m['max'], number)
            point = [row['timestamp'], row['ref']['source_file'], row['ref']['record_index'], number]
            if m['first'] is None or point[:3] < m['first'][:3]: m['first'] = point
            if m['last'] is None or point[:3] >= m['last'][:3]: m['last'] = point
        self.cache[key] = value
        if len(self.cache) > self.capacity:
            old_key, old_value = self.cache.popitem(last=False)
            self._save(old_key, old_value)

    def windows(self):
        for key, value in self.cache.items(): self._save(key, value)
        self.cache.clear()
        self.db.commit()
        for series, start, payload in self.db.execute('SELECT series,start,payload FROM flow_windows ORDER BY series,start'):
            value = load_json(payload)
            window = _Window(value['template'], datetime.fromisoformat(start.replace('Z', '+00:00')), self.width, self.step)
            window.record_count = value['count']
            window.timestamps = self.db.execute('SELECT count(*) FROM flow_times WHERE series=? AND start=?', (series, start)).fetchone()[0]
            window.flags = set(value['flags'])
            window.references = [ref['ref'] for ref in value['refs']]
            window.truncated = value['count'] > REFERENCE_LIMIT
            result = window.report()
            for name, m in sorted(value['metrics'].items()):
                metric = _Metric('netflow', name)
                metric.inputs, metric.missing, metric.invalid = m['inputs'], m['missing'], m['invalid']
                metric.finite = metric.samples.n = metric.samples.count = m['n']
                metric.samples.zeros = m['zeros']
                metric.samples.mean, metric.samples.m2 = m['mean'], m['m2'] if m['m2'] is not None else float('inf')
                metric.samples.minimum, metric.samples.maximum = m['min'], m['max']
                metric.first = m['first'][-1] if m['first'] else None
                metric.last = m['last'][-1] if m['last'] else None
                metric.total = m['total'] if m['total'] is not None else float('inf')
                report = metric.report()
                report.update(quantile_method='not_computed_streaming_netflow', quantiles_exact=False,
                              change_count=None, distinct_exact=False, distinct_count_lower_bound=None)
                result['metrics'][name] = report
            result['aggregation_policy'] = 'streaming_interval_totals_and_moments'
            yield result

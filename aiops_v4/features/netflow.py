"""Bounded disk-backed interval totals, without staging individual flows.

NetFlow state features consume sums. Counts, moments, invalid/missing inputs and
raw anchors are retained; per-flow quantiles and change counts are deliberately
not computed in this mode. Unordered and cross-file rows share the same bucket.
"""
from collections import OrderedDict
from datetime import datetime
import math
import sys
from aiops_v4.data.reader import is_missing
from .aggregate import _Window, _Metric, REFERENCE_LIMIT
from .time import iso_time, window_start
from .flow_codec import FlowCodec, _size


class NetflowAccumulator:
    def __init__(self, db, width, step, capacity=2048, cache_budget_bytes=16 * 1024**2):
        self.db, self.width, self.step, self.capacity = db, width, step, capacity
        self.cache = OrderedDict()
        self.configure_cache(capacity, cache_budget_bytes, scope='process_since_start')
        db.execute('CREATE TABLE flow_windows(series TEXT,start TEXT,payload BLOB,PRIMARY KEY(series,start)) WITHOUT ROWID')
        db.execute('CREATE TABLE flow_times(series TEXT,start TEXT,time TEXT,PRIMARY KEY(series,start,time)) WITHOUT ROWID')
        self.codec = FlowCodec(db, create=True)

    def configure_cache(self, capacity, budget_bytes, *, scope='process_since_resume'):
        if type(capacity) is not int or capacity < 1 or type(budget_bytes) is not int or budget_bytes < 1:
            raise ValueError('invalid NetFlow cache budget')
        self.capacity, self.cache_budget_bytes = capacity, budget_bytes
        self.cache_sizes = {}
        self.cache_bytes = 0
        self.cache_statistics = dict(scope=scope, hits=0, misses=0, evictions=0, reloads=0,
                                     spill_writes=0, encoded_bytes=0, peak_entries=0, peak_bytes=0)

    def _ensure_cache(self):
        if not hasattr(self, 'cache_statistics'):
            # Legacy checkpoint instances are created with __new__, not __init__.
            self.configure_cache(self.capacity, getattr(self, 'cache_budget_bytes', 16 * 1024**2))

    def cache_report(self):
        self._ensure_cache()
        return dict(self.cache_statistics, capacity=self.capacity, budget_bytes=self.cache_budget_bytes,
                    entries=len(self.cache), bytes=self.cache_bytes)

    def _value(self, key, row):
        self._ensure_cache()
        value = self.cache.pop(key, None)
        if value is not None:
            self.cache_statistics['hits'] += 1
            return value
        self.cache_statistics['misses'] += 1
        saved = self.db.execute('SELECT payload FROM flow_windows WHERE series=? AND start=?', key).fetchone()
        if saved:
            self.cache_statistics['reloads'] += 1
            return self.load_value(saved[0])
        return dict(template=row, count=0, flags=[], refs=[], metrics={})

    def _remember_value(self, key, value, refs_changed, flags_changed, new_metrics):
        self._ensure_cache()
        old = self.cache_sizes.get(key)
        refs_bytes = flags_bytes = None
        if old is None or refs_changed:
            refs_bytes = sum(_size(ref) + 128 for ref in value['refs']) + 128
        if old is None or flags_changed:
            flags_bytes = _size(value['flags']) + 256
        if old is None:
            # Reserve for changing numeric objects and first/last point alias
            # separation. Components are measured independently, conservatively
            # counting shared objects; no recursive bucket walk on every flow.
            metrics_bytes = sum(_size((name,metric)) + 512 for name,metric in value['metrics'].items())
            extras = {name:item for name,item in value.items() if name not in ('template','count','refs','flags','metrics','last_time_seen')}
            total = _size((key,value['template'],extras)) + sys.getsizeof(value) + sys.getsizeof(value['metrics']) + metrics_bytes + refs_bytes + flags_bytes + 2048
            self.cache_bytes += total
        else:
            total, old_refs, old_flags = old
            refs_bytes = old_refs if refs_bytes is None else refs_bytes
            flags_bytes = old_flags if flags_bytes is None else flags_bytes
            growth = refs_bytes - old_refs + flags_bytes - old_flags
            if new_metrics:
                growth += sum(_size((name,value['metrics'][name])) + 512 for name in new_metrics)
            total += growth
            self.cache_bytes += growth
        self.cache_sizes[key] = (total, refs_bytes, flags_bytes)
        self.cache[key] = value
        while self.cache and (len(self.cache) > self.capacity or self.cache_bytes > self.cache_budget_bytes):
            old_key, old_value = self.cache.popitem(last=False)
            self._save(old_key, old_value)
            self.cache_bytes -= self.cache_sizes.pop(old_key)[0]
            self.cache_statistics['evictions'] += 1
        self.cache_statistics['peak_entries'] = max(self.cache_statistics['peak_entries'], len(self.cache))
        self.cache_statistics['peak_bytes'] = max(self.cache_statistics['peak_bytes'], self.cache_bytes)

    def flush(self):
        self._ensure_cache()
        for key,value in self.cache.items():self._save(key,value)
        self.cache.clear();self.cache_sizes.clear();self.cache_bytes=0

    def bucket_start(self, timestamp):
        # FastFlows and legacy __new__ resume instances share this bounded cache.
        if getattr(self, 'last_stamp', None) != timestamp:
            stamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            bucket = iso_time(window_start(stamp, self.width))
            self.last_stamp, self.last_bucket = timestamp, bucket
        return self.last_bucket

    def _codec(self):
        if not hasattr(self, 'codec'):
            self.codec = FlowCodec(self.db)
        return self.codec

    def load_value(self, payload):
        return self._codec().decode(payload)

    def _save(self, key, value):
        self._ensure_cache()
        payload = self._codec().encode(value)
        self.db.execute('INSERT OR REPLACE INTO flow_windows VALUES (?,?,?)', (*key, payload))
        self.cache_statistics['spill_writes'] += 1
        self.cache_statistics['encoded_bytes'] += len(payload)

    def add(self, row):
        if row['source'] != 'netflow':
            raise ValueError('NetFlow accumulator source mismatch')
        start = self.bucket_start(row['timestamp'])
        key = row['series_key'], start
        value = self._value(key, row)
        value['count'] += 1
        flags_changed = bool(row['flags']) and any(flag not in value['flags'] for flag in row['flags'])
        if flags_changed:value['flags'] = sorted(set(value['flags']) | set(row['flags']))
        new_metrics = []
        ref = dict(time=row['timestamp'], ref=row['ref'])
        # Source files are read in deterministic order; ties use their provenance.
        order = lambda r: (r['time'], r['ref']['source_file'], r['ref']['record_index'])
        refs_changed = len(value['refs']) < REFERENCE_LIMIT or order(ref) < order(value['refs'][-1])
        if refs_changed:value['refs'] = sorted(value['refs'] + [ref], key=order)[:REFERENCE_LIMIT]
        if value.get('last_time_seen') != row['timestamp']:
            self.db.execute('INSERT OR IGNORE INTO flow_times VALUES (?,?,?)', (*key, row['timestamp']))
            value['last_time_seen'] = row['timestamp']
        for name, raw in row['raw_values'].items():
            m = value['metrics'].get(name)
            if m is None:
                new_metrics.append(name)
                m = value['metrics'][name] = dict(inputs=0, missing=0, invalid=0, n=0, zeros=0,
                           mean=0., m2=0., total=0., min=None, max=None, first=None, last=None)
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
        self._remember_value(key, value, refs_changed, flags_changed, new_metrics)

    def windows(self):
        self.flush()
        self.db.commit()
        for series, start, payload in self.db.execute('SELECT series,start,payload FROM flow_windows ORDER BY series,start'):
            value = self.load_value(payload)
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

"""Ordered views over private source spools, without a second full data copy."""
from contextlib import ExitStack, closing
from datetime import datetime
from heapq import merge
from itertools import groupby
from pathlib import Path
import sqlite3

from aiops_v4.storage import load_json
from .aggregate import _Window, _Metric, REFERENCE_LIMIT
from .spool import merge_flow
from .flow_codec import FlowCodec


def _readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA cache_size=-8192')
    return db


def iter_prepared_rows(sources, series):
    """Merge equal series by time, discovery file order and raw record index."""
    from .spool_codec import ObservationCodec, series_id
    with ExitStack() as stack:
        streams = []
        for file_index, path in sources:
            db = stack.enter_context(closing(_readonly(path)))
            identity = series_id(db, series)
            if identity is None:
                continue
            codec = ObservationCodec(db)
            def rows(db=db, codec=codec, identity=identity, file_index=file_index):
                for stamp, idx, payload in db.execute('SELECT time,idx,payload FROM observations WHERE series=? ORDER BY time,idx', (identity,)):
                    yield stamp, file_index, idx, codec.decode(identity, stamp, idx, payload)
            streams.append(rows())
        for _, _, _, row in merge(*streams, key=lambda item: item[:3]):
            yield row


def _flow_window(value, start, timestamp_count, width, step):
    window = _Window(value['template'], datetime.fromisoformat(start.replace('Z', '+00:00')), width, step)
    window.record_count = value['count']
    window.timestamps = timestamp_count
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
    return result


def iter_netflow_windows(sources, width, step, skip_series=()):
    """Merge fixed file-order contributions; count cross-file timestamps once."""
    skip_series = set(skip_series)
    with ExitStack() as stack:
        databases = {}
        codecs = {}
        streams = []
        for file_index, path in sources:
            db = stack.enter_context(closing(_readonly(path)))
            databases[file_index] = db
            codecs[file_index] = FlowCodec(db)
            def rows(db=db, file_index=file_index):
                for series, start, payload in db.execute('SELECT series,start,payload FROM flow_windows ORDER BY series,start'):
                    yield series, start, file_index, payload
            streams.append(rows())
        ordered = merge(*streams, key=lambda item: item[:3])
        for (series, start), contributions in groupby(ordered, key=lambda item: item[:2]):
            if series in skip_series:
                continue
            combined = None
            times = []
            for _, _, file_index, payload in contributions:
                value = codecs[file_index].decode(payload)
                combined = value if combined is None else merge_flow(combined, value)
                db = databases[file_index]
                times.append((row[0] for row in db.execute('SELECT time FROM flow_times WHERE series=? AND start=? ORDER BY time', (series, start))))
            timestamp_count = sum(1 for _stamp, _rows in groupby(merge(*times)))
            yield _flow_window(combined, start, timestamp_count, width, step)

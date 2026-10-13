"""Compiled CSV slots and bounded writes inside the caller's checkpoint transaction."""
from collections import OrderedDict
import sys

from aiops_v4.data.discovery import DIMENSION_FIELDS, TIME_FIELDS, numeric_columns

MAX_BATCH_ROWS = 4096
MAX_BATCH_BYTES = 4 * 1024**2
_IDENTITY = ('region_code', 'region', 'node_key', 'node', 'hostname',
             'source_region', 'source_ip', 'target_id')


def _retained_size(value, seen=None):
    seen = set() if seen is None else seen
    marker = id(value)
    if marker in seen:
        return 0
    seen.add(marker)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_retained_size(key, seen) + _retained_size(item, seen) for key, item in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        size += sum(_retained_size(item, seen) for item in value)
    return size


def _limits(max_rows, max_bytes):
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_BATCH_ROWS:
        raise ValueError('invalid ingest batch row limit')
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BATCH_BYTES:
        raise ValueError('invalid ingest batch byte limit')


class BoundedSQLBatch:
    """Flush complete INSERT batches without committing the reader's position.

    Bytes include retained Python containers and cell objects, rather than only
    compressed payload bytes. An oversized row is written on its own and never
    retained alongside pending rows. Failures keep the pending buffer intact;
    the caller rolls back the enclosing checkpoint transaction.
    """
    def __init__(self, db, sql, max_rows=MAX_BATCH_ROWS, max_bytes=MAX_BATCH_BYTES):
        _limits(max_rows, max_bytes)
        self.db, self.sql = db, sql
        self.max_rows, self.max_bytes = max_rows, max_bytes
        self.rows = []
        self.pending_bytes = 0
        self.peak_pending_rows = self.peak_pending_bytes = self.batches = 0

    @property
    def pending_rows(self):
        return len(self.rows)

    def add(self, row):
        # Conservative list-slot/container allowance remains bounded even when
        # CPython overallocates the backing list while it grows.
        size = sys.getsizeof(row) + 64
        for cell in row:
            size += sys.getsizeof(cell)
            # UTF-8 binding storage can exceed a Unicode object's native width.
            if isinstance(cell, str):size += 3 * len(cell)
        if self.rows and (len(self.rows) >= self.max_rows or self.pending_bytes + size > self.max_bytes):
            self.flush()
        if size > self.max_bytes:
            self.db.executemany(self.sql, (row,))
            self.batches += 1
            return
        self.rows.append(row)
        self.pending_bytes += size
        self.peak_pending_rows = max(self.peak_pending_rows, len(self.rows))
        self.peak_pending_bytes = max(self.peak_pending_bytes, self.pending_bytes)
        if len(self.rows) >= self.max_rows or self.pending_bytes >= self.max_bytes:
            self.flush()

    def flush(self):
        if not self.rows:
            return
        self.db.executemany(self.sql, self.rows)
        self.batches += 1
        self.rows.clear()
        self.pending_bytes = 0


class BoundedMemo:
    """Cache repeated parse results with both entry and retained-byte ceilings."""
    def __init__(self, max_rows=2048, max_bytes=MAX_BATCH_BYTES):
        _limits(max_rows, max_bytes)
        self.max_rows, self.max_bytes = max_rows, max_bytes
        self.values = OrderedDict()
        self.sizes = {}
        self.bytes = 0

    def get_or_create(self, key, create):
        value = self.values.get(key)
        if value is not None:
            return value
        value = create()
        size = _retained_size((key, value)) + 256
        if size > self.max_bytes:
            return value
        while self.values and (len(self.values) >= self.max_rows or self.bytes + size > self.max_bytes):
            old, _ = self.values.popitem(last=False)
            self.bytes -= self.sizes.pop(old)
        self.values[key] = value
        self.sizes[key] = size
        self.bytes += size
        return value


class ColumnPlan:
    """Resolve field names once for a source header, retaining CSV column order.

    General observations reuse one ephemeral raw dictionary only while invoking
    the unchanged validator and evidence-ID JSON. Prepared rows contain copies
    of the values they keep. Streaming NetFlow reads the compiled slots directly
    and never fills this dictionary or computes IDs for unretained flows.
    """
    def __init__(self, source, columns, netflow_streaming=False):
        self.source, self.columns = source, tuple(columns)
        indices = {name: index for index, name in enumerate(columns)}
        self.all = tuple(enumerate(columns))
        self.raw = dict.fromkeys(columns)
        self.time_names = TIME_FIELDS[source]
        self.time = tuple(indices.get(name, -1) for name in self.time_names)
        dimensions = ('interface_id', 'protocol') if source == 'netflow' and netflow_streaming else DIMENSION_FIELDS[source]
        names = tuple(name for name in dict.fromkeys((*_IDENTITY, *dimensions)) if name in indices)
        self.grain_names = names
        self.grain = tuple(indices.get(name, -1) for name in names)
        self.numeric = tuple((name, indices[name]) for name in numeric_columns(source, columns))
        self.traffic_numeric = {flow: tuple((name, index) for name, index in self.numeric
                                            if name.startswith(flow + '_flow_'))
                                for flow in ('dns', 'web', 'auth', 'elephant')}

    @staticmethod
    def cell(cells, index):
        return cells[index] if 0 <= index < len(cells) else None

    def values(self, cells, slots):
        length = len(cells)
        return tuple(cells[index] if 0 <= index < length else None for index in slots)

    def fill(self, cells):
        self.raw.update(zip(self.columns, cells))
        length = len(cells)
        if length < len(self.columns):
            for name in self.columns[length:]:self.raw[name] = None
        return self.raw

    def metrics(self, raw):
        if self.source == 'routing':
            metric = raw.get('metric_name')
            return {metric: raw.get('value')} if metric else {}
        names = self.numeric
        if self.source == 'traffic':
            names = self.traffic_numeric.get(raw.get('flow_type'), names)
        return {name: raw[name] for name, _ in names}

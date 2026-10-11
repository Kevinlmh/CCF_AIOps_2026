"""CSV logical records with byte boundaries; no whole-file raw evidence rescan."""
import csv
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import re
import sys

from .contracts import SourceFile
from .discovery import TIME_FIELDS, numeric_columns
from .reader import RecordReader

_NEWLINE = re.compile(b'\r\n|\r|\n')


def file_identity(path):
    stat = Path(path).stat()
    return dict(device=stat.st_dev, inode=stat.st_ino, size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def record_id(batch, path, index, raw, extra):
    content = json.dumps([raw, extra], ensure_ascii=False, separators=(',', ':'))
    value = json.dumps([batch, path, index, content], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class _PhysicalLines:
    def __init__(self, stream, offset=0, line=0, digest=None):
        self.stream, self.offset, self.line = stream, offset, line
        self.buffer, self.cursor, self.eof = b'', 0, False
        self.digest = digest or hashlib.sha256()

    def __iter__(self): return self

    def __next__(self):
        while True:
            match = _NEWLINE.search(self.buffer, self.cursor)
            if match and (match.end() < len(self.buffer) or self.eof or self.buffer[match.start():match.end()] != b'\r'):
                end = match.end(); break
            if self.eof:
                if self.cursor == len(self.buffer): raise StopIteration
                end = len(self.buffer); break
            # Read blocks once; never seek or rescan the file per physical line.
            part = self.stream.read(256 * 1024)
            self.buffer = self.buffer[self.cursor:] + part
            self.cursor = 0
            if not part:
                self.eof = True
        value = self.buffer[self.cursor:end]
        self.cursor = end
        self.digest.update(value)
        self.offset += len(value); self.line += 1
        return value.decode('utf-8-sig' if self.offset == len(value) else 'utf-8')


@dataclass(frozen=True)
class PositionedRow:
    cells: tuple
    record_index: int
    line_start: int
    line_end: int
    byte_start: int
    byte_length: int

    def raw(self, columns):
        return {name: self.cells[i] if i < len(self.cells) else None for i, name in enumerate(columns)}

    def record(self, reader):
        raw = self.raw(reader.columns);extra = self.cells[len(reader.columns):]
        normalizer = reader.normalizer
        normalizer.rows_scanned = self.record_index
        identifier = record_id(reader.batch, reader.file.relative_path, self.record_index, raw, extra)
        return normalizer._normalize(raw, extra, identifier, self.line_start, self.line_end)

    def anchor(self, reader):
        record = self.record(reader)
        return dict(record_id=record.record_id, source_file=record.source_file,
                    record_index=self.record_index, line_start=self.line_start, line_end=self.line_end,
                    byte_start=self.byte_start, byte_length=self.byte_length)


class PositionReader:
    def __init__(self, file, batch, max_rows=None, checkpoint=None):
        self.normalizer = RecordReader(file, batch, max_rows)
        self.file, self.batch, self.max_rows = file, batch, max_rows
        self.saved = checkpoint
        self.rows_scanned = 0; self.complete = False; self.schema_issues = []
        self._used = False

    def __enter__(self):
        self.identity = file_identity(self.file.path)
        self.stream = self.file.path.open('rb')
        self._field_limit = csv.field_size_limit(sys.maxsize)
        try:
            digest = hashlib.sha256()
            offset, line = 0, 0
            if self.saved:
                if self.saved['identity'] != self.identity or self.saved['batch'] != self.batch or self.saved['path'] != self.file.relative_path:
                    raise ValueError('input changed since checkpoint')
                offset, line = self.saved['byte_offset'], self.saved['physical_line']
                remaining = offset
                while remaining:
                    part = self.stream.read(min(1024*1024, remaining))
                    if not part: raise ValueError('invalid checkpoint offset')
                    digest.update(part); remaining -= len(part)
                if digest.hexdigest() != self.saved['prefix_sha256']: raise ValueError('input prefix changed')
                self.columns = list(self.saved['columns'])
                self.rows_scanned = self.saved['record_index']
                self.schema_issues = list(self.saved['schema_issues'])
            self.lines = _PhysicalLines(self.stream, offset, line, digest)
            self.csv = csv.reader(self.lines, strict=True)
            if not self.saved:
                header = next(self.csv, None); blanks = 0
                while header == []:
                    blanks += 1; header = next(self.csv, None)
                self.columns = header or []
                if blanks: self.schema_issues.append(f'leading_blank_lines:{blanks}')
                if len(set(self.columns)) != len(self.columns): raise ValueError('duplicate CSV columns')
                source = self.file.source
                if self.columns and not any(n in self.columns for n in TIME_FIELDS[source]): self.schema_issues.append('missing_timestamp_column')
                names = ('hostname','node') if source == 'frr' else ('node_key','node')
                if self.columns and source != 'traffic' and not any(n in self.columns for n in names): self.schema_issues.append('missing_entity_column')
                if self.columns and source == 'routing' and not {'metric_name','value'} <= set(self.columns): self.schema_issues.append('missing_routing_metric_columns')
            self.normalizer.columns = self.columns
            self.normalizer._numeric = numeric_columns(self.file.source, self.columns)
            return self
        except BaseException:
            self.stream.close();csv.field_size_limit(self._field_limit);raise

    def __exit__(self, *_):
        self.stream.close();csv.field_size_limit(self._field_limit)

    def __iter__(self):
        if self._used: raise RuntimeError('PositionReader may be consumed only once')
        self._used = True
        if not self.columns:
            self.complete = True;return
        while True:
            if self.max_rows is not None and self.rows_scanned >= self.max_rows:
                self.complete = self.lines.offset == self.identity['size'];return
            start, line = self.lines.offset, self.lines.line+1
            try: cells = next(self.csv, None)
            except (csv.Error, UnicodeError) as error: raise ValueError(f'invalid CSV: {error}') from error
            if cells is None:
                self.complete = True;return
            if not cells: continue
            self.rows_scanned += 1
            yield PositionedRow(tuple(cells), self.rows_scanned, line, self.lines.line,
                                start, self.lines.offset-start)

    def checkpoint(self):
        if file_identity(self.file.path) != self.identity: raise ValueError('input changed while reading')
        return dict(path=self.file.relative_path,batch=self.batch,identity=self.identity,
                    columns=self.columns,schema_issues=self.schema_issues,record_index=self.rows_scanned,
                    byte_offset=self.lines.offset,physical_line=self.lines.line,
                    prefix_sha256=self.lines.digest.hexdigest())

    def manifest(self):
        saved = self.checkpoint()
        return dict(path=self.file.relative_path,source=self.file.source,columns=self.columns,
                    rows_scanned=self.rows_scanned,complete=self.complete,schema_issues=self.schema_issues,
                    identity=self.identity,byte_range_end=saved['byte_offset'],
                    raw_bytes_sha256=saved['prefix_sha256'],digest_algorithm='sha256_raw_byte_range_v1')


def record_at(root, batch, manifest, anchor):
    root = Path(root).resolve(strict=True)
    relative = Path(manifest['path'])
    if relative.is_absolute() or '..' in relative.parts: raise ValueError('unsafe source path')
    path = root/relative
    if path.is_symlink() or root not in path.resolve(strict=True).parents: raise ValueError('unsafe source path')
    if anchor['source_file'] != manifest['path']: raise ValueError('anchor source mismatch')
    if file_identity(path) != manifest['identity']: raise ValueError('raw input changed')
    for key in ('record_index','line_start','line_end','byte_start','byte_length'):
        if type(anchor.get(key)) is not int or anchor[key] < (0 if key == 'byte_start' else 1): raise ValueError('invalid byte anchor')
    if anchor['byte_start']+anchor['byte_length'] > manifest['byte_range_end'] or anchor['line_end'] < anchor['line_start'] or anchor['record_index'] > manifest['rows_scanned']:
        raise ValueError('anchor outside scanned range')
    with path.open('rb') as stream:
        stream.seek(anchor['byte_start']); body = stream.read(anchor['byte_length'])
    field_limit = csv.field_size_limit(sys.maxsize)
    try:
        rows = list(csv.reader(io.StringIO(body.decode('utf-8'), newline=''), strict=True))
    finally: csv.field_size_limit(field_limit)
    if len(rows) != 1 or not rows[0]: raise ValueError('anchor is not one logical CSV record')
    normalizer = RecordReader(SourceFile(path,manifest['path'],manifest['source']),batch)
    normalizer.columns = manifest['columns'];normalizer._numeric = numeric_columns(manifest['source'],manifest['columns'])
    normalizer.rows_scanned = anchor['record_index']
    raw = {n: rows[0][i] if i<len(rows[0]) else None for i,n in enumerate(manifest['columns'])}
    extra = tuple(rows[0][len(manifest['columns']):])
    identifier = record_id(batch,manifest['path'],anchor['record_index'],raw,extra)
    if identifier != anchor['record_id']: raise ValueError('raw record content ID mismatch')
    if file_identity(path) != manifest['identity']: raise ValueError('raw input changed during retrieval')
    return normalizer._normalize(raw,extra,identifier,anchor['line_start'],anchor['line_end'])

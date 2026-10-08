"""Verify sparse anchors with one bounded streaming pass per source file."""
from itertools import islice
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
from aiops_v4.data.contracts import SourceFile
from aiops_v4.data.discovery import TIME_FIELDS
from aiops_v4.data.reader import RecordReader
from aiops_v4.features.time import observation_time, iso_time, time_zone
from aiops_v4.states.pipeline import _dump, _load


def validate_reference(ref, manifest):
    """Return canonical reference; source may be supplied by the upstream manifest."""
    relative = ref.get('source_file')
    if not isinstance(relative, str) or not relative:
        raise ValueError('raw reference requires source_file')
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or str(path) != relative:
        raise ValueError('raw reference path must be a canonical relative path')
    entry = manifest.get(relative)
    if entry is None or entry.get('source') not in TIME_FIELDS:
        raise ValueError('raw reference source_file is outside registered manifest')
    if ref.get('source', entry['source']) != entry['source']:
        raise ValueError('raw reference source mismatch')
    index, first, last = (ref.get(k) for k in ('record_index', 'line_start', 'line_end'))
    scanned = entry.get('rows_scanned')
    if any(type(v) is not int for v in (index, first, last, scanned)) or not 1 <= index <= scanned or not 1 <= first <= last:
        raise ValueError('raw reference row/physical lines outside upstream scan scope')
    if not isinstance(ref.get('record_id'), str) or not ref['record_id']:
        raise ValueError('raw reference record_id required')
    return {k: ref[k] for k in ('record_id', 'source_file', 'record_index', 'line_start', 'line_end')}


def resolve_references(root, batch, references, manifest, naive_timezone):
    """Disk-sort anchors, verify complete content IDs and return full RawRecords.

    Only requested rows are yielded; this is not a full raw-data snapshot.
    The caller must consume the iterator completely to validate every anchor.
    """
    root = Path(root).resolve(strict=True)
    time_zone(naive_timezone)
    if not isinstance(batch, str) or not batch.strip():
        raise ValueError('nonempty batch required')
    entries = {f['path']: f for f in manifest}
    if len(entries) != len(manifest):
        raise ValueError('duplicate raw manifest file')
    with tempfile.TemporaryDirectory(prefix='v4-raw-anchors-') as temporary:
        db = sqlite3.connect(Path(temporary) / 'anchors.sqlite')
        try:
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA cache_size=-2048')
            db.execute('CREATE TABLE anchors(id TEXT PRIMARY KEY,file TEXT,idx INTEGER,payload TEXT,UNIQUE(file,idx))')
            for ref in references:
                if ref.get('batch', batch) != batch:
                    raise ValueError('raw reference batch mismatch')
                canonical = validate_reference(ref, entries)
                payload = _dump(canonical)
                old = db.execute('SELECT payload FROM anchors WHERE id=?', (canonical['record_id'],)).fetchone()
                if old is not None:
                    if old[0] != payload:
                        raise ValueError('conflicting raw reference provenance')
                    continue
                db.execute('INSERT INTO anchors VALUES (?,?,?,?)', (canonical['record_id'], canonical['source_file'], canonical['record_index'], payload))
            db.commit()
            for relative, maximum in db.execute('SELECT file,max(idx) FROM anchors GROUP BY file ORDER BY file'):
                path = (root / relative).resolve(strict=True)
                if root not in path.parents or not path.is_file():
                    raise ValueError('raw file escapes outside registered root')
                file = SourceFile(path, relative, entries[relative]['source'])
                cursor = iter(db.execute('SELECT idx,payload FROM anchors WHERE file=? ORDER BY idx', (relative,)))
                wanted = next(cursor, None)
                with RecordReader(file, batch) as reader:
                    if 'columns' in entries[relative] and reader.columns != entries[relative]['columns']:
                        raise ValueError('raw file columns changed')
                    for record in islice(reader, maximum):
                        if wanted is None or record.record_index != wanted[0]:
                            continue
                        ref = _load(wanted[1])
                        if any(getattr(record, k) != v for k, v in ref.items()):
                            raise ValueError(f'raw record content/provenance mismatch: {relative}:{wanted[0]}')
                        observed, flags = observation_time(record, naive_timezone)
                        result = record.to_dict()
                        result.update(observed_time=iso_time(observed), time_quality_flags=list(flags))
                        yield result
                        wanted = next(cursor, None)
                if wanted is not None:
                    raise ValueError(f'raw reference record missing: {relative}:{wanted[0]}')
        except sqlite3.IntegrityError as error:
            raise ValueError('conflicting raw references for the same record') from error
        finally:
            db.close()

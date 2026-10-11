"""Lossless artifact compression; logical JSONL hashes use decompressed bytes."""
import gzip
import json
import math
from pathlib import Path
import shutil
import zlib
from contextlib import contextmanager

MAGIC = b'V4Z1'
MAX_PAYLOAD = 32 * 1024 * 1024


def load_json(value):
    if isinstance(value, bytes) and value.startswith(MAGIC):
        try:
            decoder = zlib.decompressobj()
            value = decoder.decompress(value[len(MAGIC):], MAX_PAYLOAD + 1)
            if len(value) > MAX_PAYLOAD or not decoder.eof or decoder.unused_data:
                raise ValueError('invalid or oversized compressed JSON')
        except zlib.error as error:
            raise ValueError('invalid compressed JSON') from error
    def bad(value):
        raise ValueError('nonfinite JSON value')
    def finite(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('nonfinite JSON number')
        return parsed
    return json.loads(value, parse_constant=bad, parse_float=finite)


def db_dump(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(',', ':')).encode('utf-8')
    if len(encoded) > MAX_PAYLOAD:
        raise ValueError('JSON payload exceeds storage limit')
    return MAGIC + zlib.compress(encoded, 1)


def open_jsonl(path, mode='rb'):
    path = Path(path)
    if not path.exists():
        path = Path(str(path) + '.gz')
    return gzip.open(path, mode, **({'encoding': 'utf-8'} if 't' in mode else {})) if path.suffix == '.gz' else path.open(mode, **({'encoding': 'utf-8'} if 't' in mode else {}))


def compact_jsonl(directory):
    """Called before stage publication is inventoried; never edits a sealed run."""
    for source in sorted(Path(directory).glob('*.jsonl')):
        destination = Path(str(source) + '.gz')
        with source.open('rb') as inp, destination.open('xb') as out:
            with gzip.GzipFile(filename='', mode='wb', fileobj=out, mtime=0, compresslevel=1) as packed:
                shutil.copyfileobj(inp, packed, 1024 * 1024)
        source.unlink()


def stored_path(path):
    path = Path(path)
    return path if path.exists() else Path(str(path) + '.gz')


@contextmanager
def jsonl_writer(path, compact=False):
    """Write compressed bytes from the first row, avoiding an uncompressed peak."""
    path = Path(path)
    if not compact:
        with path.open('x', encoding='utf-8') as stream:
            yield stream
    else:
        import io
        with Path(str(path)+'.gz').open('xb') as out:
            with gzip.GzipFile(filename='', mode='wb', fileobj=out, mtime=0, compresslevel=1) as packed:
                with io.TextIOWrapper(packed, encoding='utf-8') as stream:
                    yield stream

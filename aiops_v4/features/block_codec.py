"""Bounded lossless column blocks; no per-window compression or float rounding."""
import struct
import math
import sys
from functools import lru_cache
import zlib

MAX_WINDOWS = 256
# Keep repeated statistic columns within the 32KiB compression history.
# This is a soft boundary: a single legal wider window remains whole.
TARGET_BLOCK_ENTRIES = 512
MAX_BLOCK_BYTES = 8 * 1024**2
MAX_METRICS = 2048
HEADER = struct.Struct("<4sHHI")
TOKEN = struct.Struct("<4sQH")
_STORAGE = struct.Struct("<4sI")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")
TOKEN_MAGIC = b"V4T1"


def pack_token(block, row):
    if type(block) is not int or not 0 < block < 2**63 or type(row) is not int or not 0 <= row < MAX_WINDOWS:
        raise ValueError("invalid feature block token")
    return TOKEN.pack(TOKEN_MAGIC, block, row)


def unpack_token(payload):
    if not isinstance(payload, bytes) or len(payload) != TOKEN.size or not payload.startswith(TOKEN_MAGIC):
        raise ValueError("invalid feature block token")
    _, block, row = TOKEN.unpack(payload)
    if not 0 < block < 2**63 or row >= MAX_WINDOWS:
        raise ValueError("invalid feature block token")
    return block, row


def pack_storage(raw, *, compress=True):
    if not isinstance(raw, bytes) or not HEADER.size <= len(raw) <= MAX_BLOCK_BYTES:
        raise ValueError("invalid feature block size")
    return _STORAGE.pack(b"V4C1" if compress else b"V4U1", len(raw)) + (zlib.compress(raw, 1) if compress else raw)


def unpack_storage(payload, *, expected_size=None):
    if not isinstance(payload, bytes) or not _STORAGE.size <= len(payload) <= MAX_BLOCK_BYTES + 65536:
        raise ValueError("invalid stored feature block size")
    magic, size = _STORAGE.unpack_from(payload)
    if not HEADER.size <= size <= MAX_BLOCK_BYTES or (expected_size is not None and size != expected_size):
        raise ValueError("invalid declared feature block size")
    if magic == b"V4U1":
        raw = payload[_STORAGE.size:]
    elif magic == b"V4C1":
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(payload[_STORAGE.size:], size + 1)
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError("invalid compressed feature block")
        except zlib.error as error:
            raise ValueError("invalid compressed feature block") from error
    else:
        raise ValueError("unsupported feature block compression")
    if len(raw) != size:
        raise ValueError("truncated or oversized feature block")
    return raw


def fixed_statistics(value, *, finite=False):
    """One typed pack, preserving absent/null masks and unknown metadata."""
    from .store import _INT_FIELDS, _FLOAT_FIELDS, _BOOL_FIELDS, FIELD_NAMES, LAYOUT, _MISSING
    present = nonnull = 0
    values = []
    getter = value.get
    for key, mask in _INT_FIELDS:
        item = getter(key, _MISSING)
        if item is _MISSING:
            values.append(0); continue
        present |= mask
        if item is None:
            values.append(0); continue
        if not isinstance(item, int) or not -(2**63) <= item < 2**63:
            raise ValueError("statistics integer is outside int64")
        nonnull |= mask; values.append(item)
    for key, mask in _FLOAT_FIELDS:
        item = getter(key, _MISSING)
        if item is _MISSING:
            values.append(0); continue
        present |= mask
        if item is None:
            values.append(0); continue
        if finite:
            try:
                valid = math.isfinite(item)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("invalid statistics float") from error
            if not valid:
                raise ValueError("nonfinite prepared statistics number")
        nonnull |= mask; values.append(item)
    for key, mask in _BOOL_FIELDS:
        item = getter(key, _MISSING)
        if item is _MISSING:
            values.append(0); continue
        present |= mask
        if item is None:
            values.append(0); continue
        if item not in (False, True):
            raise ValueError("invalid statistics boolean")
        nonnull |= mask; values.append(bool(item))
    try:
        return LAYOUT.pack(1, present, nonnull, *values), {key: item for key, item in value.items() if key not in FIELD_NAMES}
    except (TypeError, ValueError, OverflowError, struct.error) as error:
        raise ValueError("invalid statistics numeric value") from error


def dense_size(windows, entries):
    from .store import FIELDS, INTS, FLOATS, BOOLS
    return HEADER.size + 4 * (windows + 1) + 20 * entries + 8 * len(FIELDS) + entries * (8 * (len(INTS) + len(FLOATS)) + len(BOOLS))


def encode_block(windows):
    from .store import FIELDS, INTS, FLOATS, BOOLS, LAYOUT
    count = len(windows)
    if not 1 <= count <= MAX_WINDOWS or any(len(metrics) > MAX_METRICS for metrics in windows):
        raise ValueError("invalid feature block window count")
    entries = [metric for metrics in windows for metric in metrics]
    total = len(entries)
    if dense_size(count, total) > MAX_BLOCK_BYTES:
        raise ValueError("feature block exceeds hard size limit")
    offsets = [0]
    for metrics in windows:
        offsets.append(offsets[-1] + len(metrics))
    ids = []; present = []; nonnull = []; numeric = []
    for identifier, payload in entries:
        if type(identifier) is not int or not 0 < identifier < 2**32 or not isinstance(payload, bytes) or len(payload) != LAYOUT.size or payload[0] != 1:
            raise ValueError("invalid fixed block metric")
        mask, active = struct.unpack_from("<QQ", payload, 1)
        if mask >> len(FIELDS) or active & ~mask:
            raise ValueError("invalid statistics field mask")
        ids.append(identifier); present.append(mask); nonnull.append(active); numeric.append(payload)
    head = bytearray(HEADER.pack(b"V4B1", count, len(FIELDS), total))
    head.extend(struct.pack("<" + "I" * len(offsets), *offsets))
    head.extend(struct.pack("<" + "I" * total, *ids))
    head.extend(struct.pack("<" + "Q" * total, *present))
    head.extend(struct.pack("<" + "Q" * total, *nonnull))
    # Strided byte copies run in C instead of slicing every metric for every
    # field. Keep the simpler path for tiny blocks where setup costs dominate.
    numeric_buffer = b"".join(numeric) if total >= 64 else None
    columns = []; descriptors = []; offset = len(head) + 8 * len(FIELDS); source_offset = 17
    for width in (8,) * (len(INTS) + len(FLOATS)) + (1,) * len(BOOLS):
        if numeric_buffer is None:
            pieces = [payload[source_offset:source_offset + width] for payload in numeric]
            column = (pieces[0] if pieces and all(piece == pieces[0] for piece in pieces) else b"".join(pieces))
        else:
            column = bytearray(width * total)
            for lane in range(width):
                column[lane::width] = numeric_buffer[source_offset + lane::LAYOUT.size]
            column = bytes(column)
            if column == column[:width] * total:
                column = column[:width]
        descriptors.append((offset, len(column))); columns.append(column)
        offset += len(column); source_offset += width
    for descriptor in descriptors:
        head.extend(struct.pack("<II", *descriptor))
    return bytes(head) + b"".join(columns)


class NumericBlock:
    def __init__(self, raw):
        from .store import FIELDS, INTS, FLOATS, BOOLS
        if not isinstance(raw, bytes) or not HEADER.size <= len(raw) <= MAX_BLOCK_BYTES:
            raise ValueError("invalid numeric block size")
        magic, self.count, fields, self.entries = HEADER.unpack_from(raw)
        if magic != b"V4B1" or not 1 <= self.count <= MAX_WINDOWS or fields != len(FIELDS) or self.entries > MAX_METRICS * self.count:
            raise ValueError("invalid numeric block header")
        header_size = HEADER.size + 4 * (self.count + 1) + 20 * self.entries + 8 * fields
        if header_size > len(raw):
            raise ValueError("truncated numeric block header")
        self.raw = raw
        self.offsets = struct.unpack_from("<" + "I" * (self.count + 1), raw, HEADER.size)
        if self.offsets[0] != 0 or self.offsets[-1] != self.entries or any(not 0 <= b - a <= MAX_METRICS for a, b in zip(self.offsets, self.offsets[1:])):
            raise ValueError("invalid numeric window offsets")
        self.ids_offset = HEADER.size + 4 * (self.count + 1)
        self.present_offset = self.ids_offset + 4 * self.entries
        self.nonnull_offset = self.present_offset + 8 * self.entries
        view = memoryview(raw)
        self._ids = view[self.ids_offset:self.present_offset].cast("I") if sys.byteorder == "little" else None
        self._present = view[self.present_offset:self.nonnull_offset].cast("Q") if sys.byteorder == "little" else None
        self._nonnull = view[self.nonnull_offset:self.nonnull_offset + 8 * self.entries].cast("Q") if sys.byteorder == "little" else None
        if any(self.definition(index) == 0 for index in range(self.entries)):
            raise ValueError("invalid metric dictionary ID")
        for index in range(self.entries):
            present, nonnull = self.masks(index)
            if present >> fields or nonnull & ~present:
                raise ValueError("invalid numeric field mask")
        descriptor_offset = self.nonnull_offset + 8 * self.entries
        self.columns = []
        self._values = []
        self._constants = []
        end = header_size
        for index, width in enumerate((8,) * (len(INTS) + len(FLOATS)) + (1,) * len(BOOLS)):
            offset, size = struct.unpack_from("<II", raw, descriptor_offset + 8 * index)
            expected = (0,) if self.entries == 0 else (width, width * self.entries)
            if offset != end or size not in expected or offset + size > len(raw):
                raise ValueError("invalid numeric column boundary")
            if width == 1 and any(value > 1 for value in raw[offset:offset + size]):
                raise ValueError("invalid numeric boolean value")
            self.columns.append((offset, size))
            self._constants.append(size == width)
            code = "q" if index < len(INTS) else "d" if index < len(INTS) + len(FLOATS) else "B"
            self._values.append(view[offset:offset + size].cast(code) if sys.byteorder == "little" else None)
            end += size
        if end != len(raw):
            raise ValueError("unexpected numeric block bytes")

    def masks(self, index):
        if self._present is not None:
            return self._present[index], self._nonnull[index]
        return (_U64.unpack_from(self.raw, self.present_offset + 8 * index)[0],
                _U64.unpack_from(self.raw, self.nonnull_offset + 8 * index)[0])

    def definition(self, index):
        return self._ids[index] if self._ids is not None else _U32.unpack_from(self.raw, self.ids_offset + 4 * index)[0]

    def remap(self, mapper):
        raw = bytearray(self.raw)
        saved = {}
        for index in range(self.entries):
            identifier = self.definition(index)
            if identifier not in saved:
                saved[identifier] = mapper(identifier)
            _U32.pack_into(raw, self.ids_offset + 4 * index, saved[identifier])
        return bytes(raw)

    def validate_finite(self):
        from .store import INTS, FLOATS
        for field in range(len(INTS), len(INTS) + len(FLOATS)):
            offset, size = self.columns[field]
            if not size:
                continue
            positions = (offset,) if size == 8 else range(offset, offset + size, 8)
            for index, position in enumerate(positions):
                if self.raw[position + 7] & 127 == 127 and self.raw[position + 6] & 240 == 240:
                    active = (any(self.masks(entry)[1] & (1 << field) for entry in range(self.entries))
                              if size == 8 else self.masks(index)[1] & (1 << field))
                    if active:
                        raise ValueError("nonfinite prepared statistics number")

    def decode(self, row, definitions, selection):
        from .store import INTS
        if type(row) is not int or not 0 <= row < self.count:
            raise ValueError("feature block row outside boundary")
        selected = _projection_plan(selection[1])
        result = {}
        for entry in range(self.offsets[row], self.offsets[row + 1]):
            try:
                definition = definitions[self.definition(entry)]
            except KeyError as error:
                raise ValueError("unknown block metric definition") from error
            present, nonnull = self.masks(entry)
            nulls, active = _active_plan(present, nonnull, selected)
            statistics = dict(definition["extra"])
            statistics.update(nulls)
            for index, key, boolean in active:
                values = self._values[index]
                if values is not None:
                    value = values[0 if self._constants[index] else entry]
                else:
                    width = 1 if boolean else 8
                    offset, size = self.columns[index]
                    position = offset + (width * entry if size != width else 0)
                    value = self.raw[position] if boolean else struct.unpack_from("<q" if index < len(INTS) else "<d", self.raw, position)[0]
                statistics[key] = bool(value) if boolean else value
            result[definition["name"]] = statistics
        return result


@lru_cache(maxsize=64)
def _projection_plan(fields):
    return sum(mask for _, mask, _ in fields)


@lru_cache(maxsize=256)
def _active_plan(present, nonnull, selected):
    from .store import _STATISTICS_MASKS
    nulls = tuple((key, None) for key, mask, _ in _STATISTICS_MASKS if selected & present & mask and not nonnull & mask)
    active = tuple((index, key, boolean) for index, (key, mask, boolean) in enumerate(_STATISTICS_MASKS) if selected & nonnull & mask)
    return nulls, active

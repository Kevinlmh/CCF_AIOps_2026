"""Bounded block storage preserves exact public feature semantics."""
import importlib
import importlib.util
import sqlite3
import struct
import zlib

import pytest

from aiops_v4.features import store
from aiops_v4.features.write_codec import PreparedWindowWriter, iter_metrics, pack_metrics
from aiops_v4.storage import db_dump
from test_store_writer_batch import FILES, sample, prepared


def codec():
    assert importlib.util.find_spec("aiops_v4.features.block_codec") is not None, "bounded feature block codec is missing"
    return importlib.import_module("aiops_v4.features.block_codec")


def test_new_storage_compresses_bounded_blocks_instead_of_each_window(tmp_path):
    path, db, writer = prepared(tmp_path)
    writer.add_many((sample(n) for n in range(1, 601)), batch_size=512)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 4, "new feature block format is missing"
    blocks = db.execute("SELECT windows,raw_size,payload FROM feature_blocks ORDER BY id").fetchall()
    assert len(blocks) == 3 and sum(row[0] for row in blocks) == 600
    assert all(1 <= n <= 256 and size <= 8*1024**2 for n, size, _ in blocks)
    assert all(len(token) == 14 for token, in db.execute("SELECT statistics FROM windows"))
    db.commit(); db.close()
    with store.FeatureStore(path, verify=False) as reader:
        windows = list(reader.windows(page_size=97))
        assert len(windows) == 600 and windows[0] == sample(1)[0] and windows[-1] == sample(600)[0]
        assert len(reader._block_cache) <= 2


def test_block_projection_preserves_float_bits_masks_int64_boolean_and_extras(tmp_path):
    path, db, writer = prepared(tmp_path)
    pairs = [sample(1), sample(2)]
    for n, (window, _) in enumerate(pairs):
        window["metrics"]["cpu_usage"].update(count=-(2**63) if n else 2**63-1, mean=-0.0, std=0.0,
                                              quantiles_exact=False, distinct_exact=True, first=None,
                                              unknown={"nested": [False, None, n]})
    writer.add_many(pairs); db.commit(); db.close()
    assert codec()
    with store.FeatureStore(path, verify=False) as reader:
        for actual, (expected, _) in zip(reader.windows(), pairs):
            assert actual == expected
            for key in ("mean", "std"):
                assert struct.pack("<d", actual["metrics"]["cpu_usage"][key]) == struct.pack("<d", expected["metrics"]["cpu_usage"][key])
        selected = list(reader.windows(statistics_fields=("count", "mean", "std", "first", "distinct_exact", "last"), include_references=False))
        assert set(selected[0]["metrics"]["cpu_usage"]) == {"count", "mean", "std", "first", "distinct_exact", "semantics", "unknown"}
        assert selected[0]["metrics"]["cpu_usage"]["first"] is None
        assert selected[0]["references"] == []


def test_worker_flush_and_parent_import_use_blocks_without_reencoding_numbers(tmp_path):
    path, db, writer = prepared(tmp_path)
    source = sqlite3.connect(tmp_path/"worker.sqlite")
    output = PreparedWindowWriter(source, "stage1")
    output.add(*sample(1)); output.add(*sample(2))
    assert callable(getattr(output, "flush", None)), "prepared block flush is missing"
    output.flush(); source.commit()
    assert source.execute("PRAGMA user_version").fetchone()[0] == 4
    assert source.execute("SELECT windows FROM feature_blocks").fetchall() == [(2,)]
    writer.import_prepared(source, batch_size=1); db.commit()
    assert db.execute("SELECT windows FROM feature_blocks").fetchall() == [(2,)]
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    source.close(); db.close()
    with store.FeatureStore(path, verify=False) as reader:
        assert list(reader.windows()) == [sample(1)[0], sample(2)[0]]


def test_legacy_frames_can_coexist_with_blocks_and_old_checkpoints_are_rejected(tmp_path):
    path, db, writer = prepared(tmp_path)
    writer.add_many([sample(1), sample(2)])
    assert db.execute("PRAGMA user_version").fetchone()[0] == 4, "new feature block format is missing"
    definition = db.execute("SELECT id FROM definitions").fetchone()[0]
    numeric, _ = store.pack_statistics(sample(2)[0]["metrics"]["cpu_usage"])
    db.execute("UPDATE windows SET statistics=? WHERE seq=2", (zlib.compress(pack_metrics([(definition, numeric)]), 1),))
    db.commit(); db.close()
    with store.FeatureStore(path, verify=False) as reader:
        assert list(reader.windows()) == [sample(1)[0], sample(2)[0]]
    db = sqlite3.connect(path); db.execute("PRAGMA user_version=3")
    with pytest.raises(ValueError, match="version-four"):
        store.StoreWriter.resume(db, "stage1")
    db.close()


@pytest.mark.parametrize("damage", ["oversize", "truncated", "trailing", "row", "offset", "boolean"])
def test_corrupted_block_boundaries_are_rejected_before_decoding(tmp_path, damage):
    path, db, writer = prepared(tmp_path)
    writer.add_many([sample(1), sample(2)])
    c = codec()
    block, n, raw_size, payload = db.execute("SELECT id,windows,raw_size,payload FROM feature_blocks").fetchone()
    if damage == "oversize":
        db.execute("UPDATE feature_blocks SET raw_size=?", (8*1024**2+1,))
    elif damage in ("truncated", "trailing"):
        db.execute("UPDATE feature_blocks SET payload=?", (payload[:-1] if damage == "truncated" else payload+b"x",))
    elif damage == "row":
        db.execute("UPDATE windows SET statistics=? WHERE seq=1", (c.pack_token(block, n),))
    else:
        raw = bytearray(c.unpack_storage(payload, expected_size=raw_size))
        if damage == "offset":
            struct.pack_into("<I", raw, c.HEADER.size+4, 999999)
        else:
            decoded = c.NumericBlock(bytes(raw))
            offset, _ = decoded.columns[-2]
            raw[offset] = 2
        db.execute("UPDATE feature_blocks SET payload=?", (c.pack_storage(bytes(raw)),))
    db.commit(); db.close()
    with store.FeatureStore(path, verify=False) as reader:
        with pytest.raises(ValueError):
            list(reader.windows())


def test_import_failure_rolls_back_blocks_and_resume_uses_committed_block_ids(tmp_path):
    path, db, writer = prepared(tmp_path)
    assert codec()
    writer.add(*sample(1)); db.commit()
    def failed():
        yield sample(2)
        raise RuntimeError("interrupted group")
    with pytest.raises(RuntimeError, match="interrupted"):
        writer.add_many(failed(), batch_size=1)
    assert db.execute("SELECT count(*) FROM windows").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM feature_blocks").fetchone()[0] == 1
    db.close(); db = sqlite3.connect(path)
    resumed = store.StoreWriter.resume(db, "stage1")
    resumed.add_many([sample(2), sample(3)]); db.commit(); db.close()
    with store.FeatureStore(path, verify=False) as reader:
        assert list(reader.windows()) == [sample(n)[0] for n in (1, 2, 3)]


def test_canonical_writer_flushes_before_numeric_buffer_exceeds_hard_limit(tmp_path):
    _, db, writer = prepared(tmp_path)
    def rows():
        for n in range(1, 15):
            if n == 13:
                assert db.execute("SELECT count(*) FROM windows").fetchone()[0] >= 11, "numeric buffer was not flushed at its byte limit"
            window, anchors = sample(n)
            window["metrics"] = {"metric_%d" % i: {"count": 1, "mean": -0.0, "semantics": {"kind": "excluded", "unit": "unknown", "status": "verified"}} for i in range(2048)}
            yield window, anchors
    writer.add_many(rows(), batch_size=512)
    assert all(size <= 8*1024**2 for size, in db.execute("SELECT raw_size FROM feature_blocks"))
    db.close()


@pytest.mark.parametrize("version", [1, 2, 3])
def test_real_sealed_historical_statistics_remain_readable(tmp_path, version):
    from aiops_v4.data.cache import seal_layer
    path, db, writer = prepared(tmp_path)
    window, anchors = sample(1)
    window["metrics"]["cpu_usage"]["mean"] = -0.0
    writer.add(window, anchors)
    numeric, _ = store.pack_statistics(window["metrics"]["cpu_usage"])
    identifier = db.execute("SELECT id FROM definitions").fetchone()[0]
    db.execute("UPDATE windows SET statistics=?", (zlib.compress(pack_metrics([(identifier, numeric)]), 1),))
    if version == 1:
        db.execute("UPDATE windows SET extras=(SELECT payload FROM extras WHERE id=windows.extras)")
        db.execute("UPDATE anchors SET payload=?", (db_dump(anchors[f"{10:064x}"]),))
    db.execute("DROP TABLE feature_blocks")
    db.execute("PRAGMA user_version=%d" % version); db.commit(); db.close()
    seal_layer(tmp_path, dict(batch="stage1", input_root=str(tmp_path)), signature="f"*64)
    with store.FeatureStore(path) as reader:
        actual = list(reader.windows())[0]
        assert actual == window
        assert struct.pack("<d", actual["metrics"]["cpu_usage"]["mean"]) == struct.pack("<d", -0.0)
        assert reader.get_windows([window["window_id"]]) == {window["window_id"]: window}
        assert reader.anchor(f"{10:064x}")[0] == anchors[f"{10:064x}"]


@pytest.mark.parametrize("damage", ["nonfinite", "null_mask", "block_count", "group", "unknown_definition", "unflushed"])
def test_prepared_block_damage_rolls_back_numbers_blocks_and_dictionary_ids(tmp_path, damage):
    c = codec()
    _, db, writer = prepared(tmp_path)
    source = sqlite3.connect(":memory:")
    worker = PreparedWindowWriter(source, "stage1")
    worker.add(*sample(1)); worker.flush()
    worker.add(*sample(2))
    if damage != "unflushed":
        worker.flush()
        identifier, size, payload = source.execute("SELECT id,raw_size,payload FROM feature_blocks ORDER BY id DESC LIMIT 1").fetchone()
        raw = bytearray(c.unpack_storage(payload, expected_size=size))
        block = c.NumericBlock(bytes(raw))
        if damage == "block_count":
            source.execute("UPDATE feature_blocks SET windows=3 WHERE id=?", (identifier,))
        elif damage == "group":
            source.execute("UPDATE feature_blocks SET grp=999 WHERE id=?", (identifier,))
        else:
            if damage == "nonfinite":
                offset, _ = block.columns[store.FIELDS.index("mean")]
                struct.pack_into("<d", raw, offset, float("nan"))
            elif damage == "null_mask":
                struct.pack_into("<Q", raw, block.nonnull_offset, 1 << 63)
            else:
                struct.pack_into("<I", raw, block.ids_offset, 99999)
            source.execute("UPDATE feature_blocks SET payload=? WHERE id=?", (c.pack_storage(bytes(raw), compress=False), identifier))
    source.commit()
    with pytest.raises(ValueError):
        writer.import_prepared(source, batch_size=1)
    assert db.execute("SELECT count(*) FROM windows").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM feature_blocks").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM definitions").fetchone()[0] == 0
    writer.add(*sample(3))
    assert db.execute("SELECT seq FROM windows").fetchone()[0] == 1
    source.close(); db.close()


def test_legacy_prepared_import_flushes_numeric_buffer_at_byte_limit(tmp_path):
    _, db, writer = prepared(tmp_path)
    source = sqlite3.connect(":memory:")
    output = PreparedWindowWriter(source, "stage1")
    for n in range(1, 15):
        window, anchors = sample(n)
        window["metrics"] = {"metric_%d" % i: {"count": 1, "mean": -0.0, "semantics": {"kind": "excluded"}} for i in range(2048)}
        output.add(window, anchors)
    output.flush()
    numeric, _ = store.pack_statistics({"count": 1, "mean": -0.0})
    frame = pack_metrics([(identifier, numeric) for identifier, in source.execute("SELECT id FROM definitions ORDER BY id")])
    source.execute("UPDATE windows SET statistics=?", (frame,)); source.execute("PRAGMA user_version=3"); source.commit()
    class ReadingBoundary:
        def execute(self, sql, args=()):
            result = source.execute(sql, args)
            if "FROM windows ORDER BY position" not in sql:
                return result
            def rows():
                for number, row in enumerate(result, 1):
                    if number == 13:
                        assert db.execute("SELECT count(*) FROM windows").fetchone()[0] >= 11, "legacy numeric buffer exceeded its byte limit"
                    yield row
            return rows()
    writer.import_prepared(ReadingBoundary(), batch_size=512)
    assert db.execute("SELECT count(*) FROM windows").fetchone()[0] == 14
    source.close(); db.close()


@pytest.mark.parametrize("prepared_worker", [False, True])
def test_many_metric_windows_keep_repeated_columns_inside_compression_history(tmp_path, prepared_worker):
    """A wide group must avoid repeating float columns beyond zlib history."""
    import hashlib
    pairs = []
    for number in range(1, 257):
        window, anchors = sample(number)
        window["metrics"] = {}
        for metric in range(31):
            mantissa = int.from_bytes(hashlib.sha256(("%d:%d" % (number, metric)).encode()).digest()[:8], "little") & (2**52-1)
            value = struct.unpack("<d", struct.pack("<Q", 0x3ff0000000000000 | mantissa))[0]
            statistics = dict.fromkeys(store.INTS, 1)
            statistics.update(dict.fromkeys(store.FLOATS, value))
            statistics.update(std=0.0, mad=0.0, missing_fraction=0.0, first=-0.0, median=None,
                              quantiles_exact=True, distinct_exact=False,
                              semantics={"kind": "gauge", "unit": "unknown", "status": "verified"},
                              unknown={"preserved": [None, False, metric]})
            window["metrics"]["metric_%d" % metric] = statistics
        pairs.append((window, anchors))
    path, db, writer = prepared(tmp_path)
    if prepared_worker:
        source = sqlite3.connect(":memory:")
        worker = PreparedWindowWriter(source, "stage1")
        for pair in pairs:
            worker.add(*pair)
        worker.flush()
        writer.import_prepared(source)
        source.close()
    else:
        writer.add_many(pairs)
    compressed = db.execute("SELECT sum(length(payload)) FROM feature_blocks").fetchone()[0]
    assert compressed < 110000, "repeated float columns escaped the compression history"
    db.commit(); db.close()
    with store.FeatureStore(path, verify=False) as reader:
        actual = list(reader.windows())
        assert actual == [window for window, _ in pairs]
        for decoded, (original, _) in zip(actual, pairs):
            for key, value in decoded["metrics"].items():
                assert struct.pack("<d", value["first"]) == struct.pack("<d", original["metrics"][key]["first"])


def test_single_large_window_can_exceed_compression_soft_boundary(tmp_path):
    path, db, writer = prepared(tmp_path)
    window, anchors = sample(1)
    window["metrics"] = {"metric_%d" % n: {"mean": -0.0, "count": 2**63-1, "median": None, "semantics": {"kind": "excluded", "unit": "unknown", "status": "verified"}} for n in range(2048)}
    writer.add(window, anchors)
    db.commit(); db.close()
    with store.FeatureStore(path, verify=False) as reader:
        assert list(reader.windows()) == [window]


def test_large_block_transposes_numbers_without_per_entry_field_slices():
    """Python slice work must stay independent of metric count."""
    class StatisticsBytes(bytes):
        field_slices = 0

        def __getitem__(self, key):
            if isinstance(key, slice):
                type(self).field_slices += 1
            return super().__getitem__(key)

    c = codec()
    windows = []
    definitions = {1: {"name": "metric", "extra": {}}}
    for row in range(256):
        numeric, _ = c.fixed_statistics({"count": -(2**63) if row % 2 else 2**63-1,
                                          "mean": -0.0 if row % 2 else row + 0.125,
                                          "first": None, "last": row + 0.25,
                                          "distinct_exact": bool(row % 2)})
        windows.append([(1, StatisticsBytes(numeric))])
    raw = c.encode_block(windows)
    assert StatisticsBytes.field_slices < 64, "numeric transposition still slices every field of every metric"
    block = c.NumericBlock(raw)
    for row in range(256):
        metric = block.decode(row, definitions, store._statistics_layout(None))["metric"]
        assert metric["count"] == (-(2**63) if row % 2 else 2**63-1)
        assert struct.pack("<d", metric["mean"]) == struct.pack("<d", -0.0 if row % 2 else row + 0.125)
        assert metric["first"] is None
        assert metric["last"] == row + 0.25
        assert metric["distinct_exact"] is bool(row % 2)

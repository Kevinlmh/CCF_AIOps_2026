
"""Block state payloads retain the public APIs, decisions and exact values."""
from importlib import import_module,util
import sqlite3
import struct
import pytest
from state_helpers import window
from aiops_v4.states.matrix import window_vector
from aiops_v4.states.codec import pack_delta,unpack_delta
from aiops_v4.features.build import build_feature_store
from aiops_v4.states import build
from aiops_v4.states.store import StateStore
from test_evidence_pipeline import inputs
from test_feature_store import compare


def module():
    assert util.find_spec("aiops_v4.states.blocks"), "bounded state blocks are missing"
    return import_module("aiops_v4.states.blocks")


def test_state_blocks_preserve_raw_typed_scalar_values_and_reject_corruption():
    blocks=module();strings={}
    def intern(value):
        if value not in strings:strings[value]=len(strings)+1
        return strings[value]
    values=[dict(x=-0.0,y=float.fromhex("0x1.123456789abcdep+12"),n=2**70+i) for i in range(9)]
    raw=[pack_delta(value,intern,compress=False) for value in values]
    block=blocks.pack_block(raw)
    recovered=blocks.unpack_block(block)
    assert recovered==tuple(raw)
    names={identifier:name for name,identifier in strings.items()}
    for expected,payload in zip(values,recovered):
        actual=unpack_delta(payload,names)
        assert struct.pack("<d",actual["x"])==struct.pack("<d",expected["x"])
        assert struct.pack("<d",actual["y"])==struct.pack("<d",expected["y"])
        assert actual["n"]==expected["n"]
    for bad in (block[:-1],block+b"junk",b"V4B1"+b"\xff"*12):
        with pytest.raises(ValueError):blocks.unpack_block(bad)


def test_state_build_stores_one_block_payload_and_reconstructs_all_public_reads(tmp_path):
    raw,_,old=inputs(tmp_path,interfaces=3)
    feature=tmp_path/"dataset/features";out=feature.parent/"states"
    build_feature_store(raw,"stage2",feature,workers=1,min_free_bytes=0,naive_timezone="UTC",netflow_streaming=False)
    build.build_state_store(feature,"stage2",out,workers=1,min_free_bytes=0,reference_start="2026-09-17T04:00:00Z",reference_end="2026-09-17T04:12:00Z")
    with sqlite3.connect(out/"states.sqlite") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0]==3
        assert db.execute("SELECT count(*) FROM state_blocks").fetchone()[0]<64
        assert db.execute("SELECT count(*) FROM deltas WHERE payload IS NOT NULL").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM deltas WHERE block IS NULL OR position IS NULL").fetchone()[0]==0
    import json
    with StateStore(out/"states.sqlite") as store:
        for name in ("states","matrix","models","events"):
            compare([json.loads(line) for line in (old/(name+".jsonl")).read_text().splitlines()],list(store.rows(name,page_size=3)))
        selected=store.query_states(entity_id="xian-br-1",source="node",limit=7)
        assert len(selected)==7
        assert store.pair(selected[0][0]["vector_id"])==selected[0]
        with sqlite3.connect(":memory:") as joined:
            store.attach_views(joined)
            assert joined.execute("SELECT count(*) FROM states").fetchone()[0]==64
            assert joined.execute("SELECT count(*) FROM states WHERE status='scored'").fetchone()[0]>0
            assert joined.execute("SELECT count(*) FROM event_windows").fetchone()[0]==11
            assert joined.execute("SELECT payload FROM states WHERE id=?",(selected[0][0]["vector_id"],)).fetchone()

"""One state projection scan is replayed without reordering or float changes."""
from importlib import import_module,util
import struct
import pytest
from state_helpers import window
from aiops_v4.states.matrix import window_vector
from aiops_v4.states.reference import fit_reference


def module():
    assert util.find_spec("aiops_v4.states.vector_batch"), "bounded vector replay is missing"
    return import_module("aiops_v4.states.vector_batch")


def bits(value):
    if type(value) is float:return ("float",struct.pack("<d",value))
    if isinstance(value,dict):return {k:bits(v) for k,v in value.items()}
    if isinstance(value,list):return [bits(v) for v in value]
    return value


@pytest.mark.parametrize("memory_limit",[1,32*1024*1024])
def test_replay_retains_order_references_and_ieee_values_with_bounded_spill(tmp_path,memory_limit):
    rows=[window_vector(window(i,value=-0.0 if i==0 else i+0.125),"stage2") for i in range(19)]
    reads=[]
    def source():
        for row in rows:reads.append(row["vector_id"]);yield row
    path=tmp_path/"vectors.bin"
    with module().VectorReplay(path,page_size=3,memory_limit=memory_limit) as replay:
        reference=fit_reference(replay.capture(source()),min_reference=2)
        assert reads==[row["vector_id"] for row in rows]
        assert bits(list(replay.rows()))==bits(rows)
        assert bits(list(replay.rows()))==bits(rows)
        assert reference==fit_reference(iter(rows),min_reference=2)
        assert replay.retained_bytes<=memory_limit
        assert path.exists()==(memory_limit==1)
    assert not path.exists()


def test_projection_spool_is_removed_after_source_failure(tmp_path):
    path=tmp_path/"vectors.bin"
    def source():
        yield window_vector(window(0),"stage2")
        yield window_vector(window(1),"stage2")
        raise RuntimeError("injected source failure")
    with pytest.raises(RuntimeError,match="injected"):
        with module().VectorReplay(path,page_size=1,memory_limit=1) as replay:
            list(replay.capture(source()))
    assert not path.exists()


def test_compute_group_projects_each_window_once(tmp_path,monkeypatch):
    from aiops_v4.features.build import build_feature_store
    from aiops_v4.features.store import FeatureStore
    from aiops_v4.states import build
    from test_records import write_csv
    raw=tmp_path/"raw"
    write_csv(raw,"node_metrics.csv",[dict(timestamp=f"2026-09-17T04:{i:02d}:00Z",node="br1",cpu_usage=str(i)) for i in range(16)])
    feature=tmp_path/"features"
    build_feature_store(raw,"stage1",feature,workers=1,min_free_bytes=0,naive_timezone="UTC")
    original=build.window_vector;calls=[]
    def project(row,batch):calls.append(row["window_id"]);return original(row,batch)
    monkeypatch.setattr(build,"window_vector",project)
    options=dict(reference_start=None,reference_end=None,min_reference=12,clusters=3,mode="hybrid",stat_threshold=6.,cluster_threshold=3.,rare_fraction=.1,minimum_overlap=.5,rules_enabled=True,candidate_policy="legacy",candidate_min_windows=2)
    with FeatureStore(feature/"features.sqlite") as reader:
        group,identifier=reader.db.execute("SELECT state_group,id FROM groups").fetchone()
        build.compute_group((group,identifier,options,str(tmp_path/"worker.sqlite")),reader)
    assert len(calls)==len(set(calls))==16

def test_scoring_replay_keeps_resource_and_cancellation_checks(tmp_path):
    import hashlib
    from aiops_v4.states import build
    class Features:
        summary={"batch":"stage2"}
        def windows(self,*args,**kwargs):
            for index in range(300):
                row=window(index,value=index%31);row["window_id"]=hashlib.sha256(str(index).encode()).hexdigest()
                yield row
    class Policy:
        checks=0
        def check(self):
            self.checks+=1
            if self.checks==2:raise RuntimeError("scoring resource check")
    policy=Policy()
    options=dict(reference_start=None,reference_end=None,min_reference=12,clusters=3,mode="hybrid",stat_threshold=6.,cluster_threshold=3.,rare_fraction=.1,minimum_overlap=.5,rules_enabled=True,candidate_policy="legacy",candidate_min_windows=2)
    with pytest.raises(RuntimeError,match="scoring resource check"):
        build.compute_group(("group",1,options,str(tmp_path/"worker.sqlite")),Features(),policy)
    assert not (tmp_path/"worker.vectors").exists()



def test_memory_replay_avoids_spill_serialization_and_isolates_mutable_rows(tmp_path,monkeypatch):
    replay_module=module()
    def unavailable_codec(page):
        raise AssertionError("in-memory vectors were serialized")
    monkeypatch.setattr(replay_module,"_encode",unavailable_codec)
    rows=[window_vector(window(i,value=i+0.125),"stage2") for i in range(5)]
    expected=bits(rows)
    with replay_module.VectorReplay(tmp_path/"vectors.bin",page_size=2) as replay:
        list(replay.capture(iter(rows)))
        rows[0]["references"][0]["source_file"]="changed input"
        rows[0]["identity"]["entity_id"]="changed input"
        recovered=list(replay.rows())
        assert bits(recovered)==expected
        recovered[0]["references"][0]["source_file"]="changed replay"
        recovered[0]["quality"]["flags"].append("changed replay")
        recovered[0]["identity"]["entity_id"]="changed replay"
        recovered[0]["excluded_metrics"].append("changed replay")
        assert bits(list(replay.rows()))==expected
        assert not replay.path.exists()


def test_replay_cache_counts_large_metadata_before_spill_and_preserves_values(tmp_path):
    rows=[window_vector(window(i,value=i+0.125),"stage2") for i in range(6)]
    for row in rows:
        row["references"][0]["source_file"]="q"*20000
    expected=bits(rows)
    with module().VectorReplay(tmp_path/"vectors.bin",page_size=2,memory_limit=50000) as replay:
        list(replay.capture(iter(rows)))
        assert replay.path.exists(),"uncompressed retained metadata must trigger the memory budget"
        assert replay.retained_bytes<=50000
        assert bits(list(replay.rows()))==expected


def test_spill_encoder_preserves_retained_page_for_retries(tmp_path):
    replay_module=module()
    rows=[window_vector(window(i,value=i+0.125),"stage2") for i in range(3)]
    page=replay_module._compact(rows)
    first=replay_module._encode(page)
    assert replay_module._encode(page)==first
    assert bits(list(replay_module._expand(page)))==bits(rows)


def test_retained_memory_accounting_covers_numeric_and_nested_reference_heap(tmp_path):
    import sys
    rows=[window_vector(window(i,value=i+0.125),"stage2") for i in range(9)]
    for row in rows:
        row["references"][0]["source_file"]="引用"*1000
        row["references"][0]["nested"]={"tags":["alpha","beta"]}
    def heap(value,seen):
        if id(value) in seen:return 0
        seen.add(id(value));size=sys.getsizeof(value)
        if isinstance(value,dict):return size+sum(heap(key,seen)+heap(item,seen) for key,item in value.items())
        if isinstance(value,(list,tuple)):return size+sum(heap(item,seen) for item in value)
        return size
    with module().VectorReplay(tmp_path/"vectors.bin",page_size=2) as replay:
        list(replay.capture(iter(rows)))
        assert heap(replay.pages,set())<=replay.retained_bytes<=replay.memory_limit
        assert bits(list(replay.rows()))==bits(rows)


def test_memory_replay_snapshots_mutable_numeric_extras(tmp_path):
    row=window_vector(window(),"stage2")
    row["features"]["cpu_usage::mean"]["value"]={"nested":[1,-0.0]}
    row["features"]["cpu_usage::mean"]["raw_value"]=[True,2**70]
    row["rule_inputs"]["cpu_usage"]["last"]={"nested":[2]}
    expected=bits(row)
    with module().VectorReplay(tmp_path/"vectors.bin",page_size=1) as replay:
        list(replay.capture(iter([row])))
        row["features"]["cpu_usage::mean"]["value"]["nested"].append(5)
        actual=list(replay.rows())[0]
        assert bits(actual)==expected
        actual["features"]["cpu_usage::mean"]["raw_value"].append(7)
        actual["rule_inputs"]["cpu_usage"]["last"]["nested"].append(8)
        assert bits(list(replay.rows())[0])==expected


def test_numeric_pages_fit_small_cache_without_python_number_lists(tmp_path):
    rows=[]
    for index in range(24):
        source=window(index,value=index+0.125)
        source["metrics"]={"metric_%03d"%metric:dict(source["metrics"]["cpu_usage"]) for metric in range(128)}
        rows.append(window_vector(source,"stage2"))
    with module().VectorReplay(tmp_path/"vectors.bin",page_size=8,memory_limit=2*1024*1024) as replay:
        list(replay.capture(iter(rows)))
        assert not replay.path.exists(),"typed numeric pages should fit the cache budget"
        assert replay.retained_bytes<=2*1024*1024
        assert bits(list(replay.rows()))==bits(rows)


@pytest.mark.parametrize("memory_limit",[1,32*1024*1024])
def test_typed_replay_preserves_category_large_integer_boolean_and_grain_bits(tmp_path,memory_limit):
    rows=[window_vector(window(i,value=-0.0 if i==0 else i+0.125),"stage2") for i in range(4)]
    for index,row in enumerate(rows):
        row["features"]["cpu_usage::mean"]["value"]="1" if index%2 else None
        row["features"]["cpu_usage::mean"]["raw_value"]=2**70+index
        row["rule_inputs"]["cpu_usage"]["last"]=2**63+index
        row["rule_inputs"]["cpu_usage"]["min"]=bool(index%2)
        row["dimensions"]["offset"]=-0.0 if index%2 else 0.0
    with module().VectorReplay(tmp_path/"vectors.bin",page_size=4,memory_limit=memory_limit) as replay:
        list(replay.capture(iter(rows)))
        assert bits(list(replay.rows()))==bits(rows)
        assert bits(list(replay.rows()))==bits(rows)


def test_spill_transition_preserves_earlier_cached_pages(tmp_path):
    rows=[window_vector(window(i,value=i+0.125),"stage2") for i in range(8)]
    with module().VectorReplay(tmp_path/"vectors.bin",page_size=1,memory_limit=24000) as replay:
        source=replay.capture(iter(rows))
        first=[next(source) for _ in range(3)]
        assert replay.pages and not replay.path.exists()
        assert first+list(source)==rows
        assert replay.path.exists() and not replay.pages
        assert bits(list(replay.rows()))==bits(rows)


def test_oversize_spill_pages_split_and_single_row_failure_cleans_up(tmp_path):
    import hashlib
    rows=[window_vector(window(i,value=i+0.125),"stage2") for i in range(8)]
    for index,row in enumerate(rows):
        row["references"][0]["source_file"]="".join(hashlib.sha256(f"{index}:{part}".encode()).hexdigest() for part in range(5))
    path=tmp_path/"vectors.bin"
    with module().VectorReplay(path,page_size=8,memory_limit=1,page_bytes=1200) as replay:
        list(replay.capture(iter(rows)))
        assert bits(list(replay.rows()))==bits(rows)
    assert not path.exists()
    with pytest.raises(ValueError,match="vector exceeds replay page byte limit"):
        with module().VectorReplay(path,page_size=1,memory_limit=1,page_bytes=32) as replay:
            list(replay.capture(iter(rows[:1])))
    assert not path.exists()

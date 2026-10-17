"""Optional exact-reference transport; facts and recorded protocols stay intact."""
import copy
import hashlib
import struct
import types
from pathlib import Path

import pytest

from aiops_v4.agents.backend import HTTPBackend, Completion
from aiops_v4.agents.engine import recorded_delivery
from aiops_v4.agents.jsonio import dumps, loads
from aiops_v4.agents.wire import pack_request, unpack_request, request_stats
from test_agent_tools import evidence
from test_structured_roles import StructuredReplay, fixture_role, good_output

V1 = "v4-evidence-wire-1"
V2 = "v4-evidence-wire-2"


def fact(identifier="a" * 64):
    return dict(vector_id=identifier, batch="stage1", source="node", entity_id="xian-br-1",
        feature_values={"metric_" + str(i): dict(raw_value=float(i), unit="count", semantic_status="registered") for i in range(24)},
        quality_flags=[], provenance=dict(source="raw.csv", line=12), missingness=dict(known=None))


def request():
    value = fact()
    return dict(model="m", messages=[dict(role="system",content="judge evidence"),
        dict(role="user",content=dumps(dict(support=[value],quality_context=[copy.deepcopy(value)])))],tools=[])


def check(original):
    wire, codec = pack_request(original,version=V2)
    assert dumps(unpack_request(wire,codec)) == dumps(original)
    assert dumps(unpack_request(wire,codec)) == dumps(original)
    return wire, codec


def test_opt_in_exact_pool_is_smaller_without_changing_v1_default():
    original=request();before,c1=pack_request(original);after,c2=check(original)
    assert request_stats(original,before,c1)["version"]==V1
    assert request_stats(original,after,c2)["version"]==V2
    assert len(dumps(after)) < len(dumps(before))
    assert "$v4_fact_ref" in dumps(after)
    assert original==request()


@pytest.mark.parametrize("change", ["extra_unit","extra_quality","extra_provenance","value","absent_vs_null"])
def test_same_id_different_complete_fields_are_never_merged(change):
    original=request();body=loads(original["messages"][1]["content"])
    row=body["quality_context"][0]
    if change=="extra_unit":row["feature_values"]["new"]={"raw_value":1,"unit":"seconds","semantic_status":"inferred"}
    elif change=="extra_quality":row["quality_flags"]=["missing"]
    elif change=="extra_provenance":row["provenance"]["sha256"]="b"*64
    elif change=="value":row["feature_values"]["metric_0"]["raw_value"]=-99.0
    else:del row["missingness"]["known"]
    original["messages"][1]["content"]=dumps(body)
    check(original)


def test_numeric_types_and_negative_zero_are_preserved():
    original=request();body=loads(original["messages"][1]["content"])
    body["typed"]=[False,0,0.0,-0.0,None,{},[]]
    original["messages"][1]["content"]=dumps(body)
    wire,codec=check(original);back=loads(unpack_request(wire,codec)["messages"][1]["content"])
    assert [type(v) for v in back["typed"][:4]]==[bool,int,float,float]
    assert struct.pack(">d",back["typed"][3])==struct.pack(">d",-0.0)


def test_incomplete_chunk_stays_identical_to_v1_and_never_becomes_complete():
    original=request();original["messages"].append(dict(role="tool",tool_call_id="call",content=dumps(dict(complete=False,text='[{"vector_id":"'+"a"*64+'",',offset=0,total_chars=12345,next_offset=12000,handle="chunk"))))
    before,_=pack_request(original);after,_=check(original)
    assert before["messages"][-1]["content"]==after["messages"][-1]["content"]


def test_complete_embedded_tool_json_deduplicates_but_roundtrips_text_exactly():
    original=request();original["messages"].append(dict(role="tool",tool_call_id="call",content=dumps(dict(complete=True,text=dumps([fact()]),offset=0,total_chars=len(dumps([fact()])),next_offset=None,handle="chunk"))))
    check(original)


@pytest.mark.parametrize("tag",["$v4_fact_ref","$v4_fact_def","$v4_fact_literal"])
def test_original_reference_tags_are_literal_even_if_malicious(tag):
    original=request();body=loads(original["messages"][1]["content"])
    body["untrusted"]={tag:"f0","value":{"$v4_fact_ref":"f999"},"raw":"@@h0 @h0 "+"c"*64}
    original["messages"][1]["content"]=dumps(body)
    check(original)


def find_key(value,key):
    if isinstance(value,dict):
        if key in value:return value
        for child in value.values():
            result=find_key(child,key)
            if result is not None:return result
    elif isinstance(value,list):
        for child in value:
            result=find_key(child,key)
            if result is not None:return result


@pytest.mark.parametrize("mutation",["unknown","cycle","duplicate"])
def test_invalid_reference_graph_is_rejected(mutation):
    wire,codec=pack_request(request(),version=V2);body=loads(wire["messages"][1]["content"])
    ref=find_key(body,"$v4_fact_ref");definition=find_key(body,"$v4_fact_def")
    assert ref is not None and definition is not None
    if mutation=="unknown":ref["$v4_fact_ref"]="f999999"
    elif mutation=="cycle":definition["value"]={"$v4_fact_ref":definition["$v4_fact_def"]}
    else:ref.clear();ref.update(copy.deepcopy(definition))
    wire["messages"][1]["content"]=dumps(body)
    with pytest.raises(ValueError):unpack_request(wire,codec)


def test_v2_is_explicit_backend_option_and_default_remains_v1():
    default=HTTPBackend("m","http://127.0.0.1:8000/v1","local",structured_roles=True,compact_evidence=True)
    selected=HTTPBackend("m","http://127.0.0.1:8000/v1","local",structured_roles=True,compact_evidence=True,evidence_wire_version=V2)
    assert default.metadata()["evidence_wire"]==V1
    assert selected.metadata()["evidence_wire"]==V2
    with pytest.raises(ValueError):HTTPBackend("m","http://127.0.0.1:8000/v1","local",structured_roles=True,compact_evidence=True,evidence_wire_version="unknown")


def test_frozen_v1_wire_and_statistics_stay_identical():
    path=Path(__file__).resolve().parents[2]/"outputs/v4/full_stage1_llm/serving_tuning/dedup/wire.py.before"
    old=types.ModuleType("old_wire");old.__package__="aiops_v4.agents";exec(compile(path.read_text(),str(path),"exec"),old.__dict__)
    original=request();before,c0=old.pack_request(original);after,c1=pack_request(original,version=V1)
    assert dumps(before)==dumps(after)
    assert old.request_stats(original,before,c0)==request_stats(original,after,c1)


def test_v2_recorded_delivery_reconstructs_actual_selected_protocol(evidence,monkeypatch):
    backend=HTTPBackend("fixture-model","http://127.0.0.1:8000/v1","fixture",structured_roles=True,compact_evidence=True,
        evidence_wire_version=V2,enable_thinking=False,temperature=0,max_completion_tokens=1536)
    original=StructuredReplay.complete
    monkeypatch.setattr(StructuredReplay,"metadata",lambda self:backend.metadata())
    def complete(self,messages,tools,**kwargs):
        body=backend._payload(messages,tools);wire,codec=pack_request(body,version=V2)
        response=original(self,messages,tools,**kwargs)
        return Completion(response.message,response.usage,response.model,request_stats(body,wire,codec))
    monkeypatch.setattr(StructuredReplay,"complete",complete)
    _,run=fixture_role(evidence,lambda session,manifest:[good_output(session,manifest)])
    assert run["status"]=="completed" and run["backend_calls"]==1
    recorded_delivery(run,"confirmation","case")

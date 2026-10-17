import copy
import json
import pytest
from test_agent_tools import evidence
from aiops_v4.agents.structured import (EVIDENCE_FIRST_KEYS,evidence_first_schema,evidence_first_arguments,canonical_result,result_tool,validate_result)
from aiops_v4.agents.engine import run_role,recorded_delivery
from aiops_v4.agents.tools import EvidenceSession
from aiops_v4.agents.contracts import load_manifest
from aiops_v4.agents.backend import ReplayBackend

PAYLOAD={"bundle":{"a1_role_contract":"simple-v2","root_ranking_policy":"ranked5-v2","initial_evidence_profile":"a1-readable-1"}}

def encoded(x):
    if isinstance(x,list):return [encoded(v) for v in x]
    if not isinstance(x,dict):return x
    return {EVIDENCE_FIRST_KEYS.get(k,k):encoded(v) for k,v in x.items()}

def candidates():
    names=["xian-br-1","xian-br-2","xian-service-vm-1","xian-service-vm-2","xian-service-vm-3"]
    return [dict(network_element_id=n,confidence=.8-i*.1,reason="Observed candidate, not proof of fault",support=[dict(kind="state",id=str(i),claim="Own measured observation")],counter_citations=[]) for i,n in enumerate(names)]

def test_native_five_schema_and_bound_observations():
    rows=candidates();seen={("state",str(i)):dict(entity_id=r["network_element_id"],source="node",candidate=True) for i,r in enumerate(rows)}
    tool=result_tool("localization",PAYLOAD,seen)["function"]["parameters"]
    resolved=next(o for o in tool["anyOf"] if o["properties"]["z_decision"]["enum"]==["resolved"])
    arr=resolved["properties"]["candidates_by_device"];assert len(arr["required"])==5 and set(arr["required"])==set(arr["properties"])
    for node,branch in arr["properties"].items():
        props=branch["properties"]
        ids=props["b_support"]["items"]["anyOf"][0]["properties"]["a_id"]["enum"]
        assert all(seen[("state",i)]["entity_id"]==node for i in ids)
    pool={r["network_element_id"]:{k:v for k,v in r.items() if k!="network_element_id"} for r in rows}
    args=encoded(dict(decision="resolved",candidates_by_device=pool,preferred_device=rows[0]["network_element_id"],reason="ranked shortlist",missing_evidence=[]))
    out=canonical_result("localization",args,PAYLOAD);validate_result("localization",out,PAYLOAD,seen)
    assert len(out["candidates"])==5
    assert [r["network_element_id"] for r in out["candidates"]]==[r["network_element_id"] for r in rows]
    args["candidates_by_device"].pop(rows[0]["network_element_id"])
    with pytest.raises(ValueError,match="five distinct"):canonical_result("localization",args,PAYLOAD)

def test_evidence_and_reason_precede_decision_under_canonical_sort():
    schema=evidence_first_schema(dict(type="object",properties={"support":{},"reason":{},"decision":{}},required=["support","reason","decision"]))
    keys=sorted(schema["properties"]);assert keys==["b_support","e_reason","z_decision"]
    assert evidence_first_arguments(encoded({"id":"real","reason":"observed","decision":"rejected"}))=={"id":"real","reason":"observed","decision":"rejected"}
    with pytest.raises(ValueError,match="duplicate"):evidence_first_arguments({"reason":"x","e_reason":"y"})

def test_confirmation_no_empty_group_and_protocol_replay(evidence):
    from aiops_v4.agents.partition import observed_components
    db,packet=evidence;packet=dict(packet,initial_evidence_profile="a1-readable-1")
    manifest=load_manifest(db,"stage2",packet["bundle_id"]);session=EvidenceSession(db,"stage2",packet)
    session.prepare_candidates([w["vector_id"] for w in manifest]);visible=session.packet["candidate_observations"][0]["vector_id"]
    item=dict(decision="confirmed",confidence=.8,reason="Fixture only",support=[dict(kind="state",id=visible,claim="Observed support")],mechanism_support=[],counter_citations=[],missing_evidence=[])
    payload=dict(bundle=packet,manifest=manifest);groups=observed_components(manifest);assert len(groups)==1
    schema=result_tool("confirmation",dict(payload,bundle=session.packet),session.seen)["function"]["parameters"]
    assert set(schema["properties"])=={"component_assessments"}
    assert schema["properties"]["component_assessments"]["required"]==["c0"]
    class Structured(ReplayBackend):
        structured_roles=True
        def metadata(self):return dict(super().metadata(),structured_roles=True)
    args=encoded(dict(component_assessments={"c0":item}))
    backend=Structured("fixture",[dict(role="confirmation",case_id="fixture",turn=0,message=dict(role="assistant",content=None,tool_calls=[dict(id="final",type="function",function=dict(name="submit_role_result",arguments=json.dumps(args)))]))])
    run=run_role(backend,EvidenceSession(db,"stage2",packet),"confirmation","fixture",payload)
    assert run["status"]=="completed",run["error_code"]
    recorded_delivery(run,"confirmation","fixture")
    empty=copy.deepcopy(args);empty["component_assessments"]={}
    with pytest.raises(ValueError,match="component"):canonical_result("confirmation",empty,dict(payload,bundle=session.packet))

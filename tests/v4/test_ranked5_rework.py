import copy
import json
import pytest
from test_agent_tools import evidence
from aiops_v4.agents.backend import ReplayBackend
from aiops_v4.agents.engine import run_role, recorded_delivery
from aiops_v4.agents.tools import EvidenceSession
from aiops_v4.agents.structured import canonical_result

def call(name,args):
    return dict(role="assistant",content=None,tool_calls=[dict(id="q",type="function",function=dict(name=name,arguments=args))])

def test_malformed_query_can_be_repaired_and_audited(evidence):
    db,packet=evidence
    api=ReplayBackend("fixture",[dict(role="confirmation",case_id="x",turn=0,message=call("query_states",json.dumps(dict(start="2026-08-27T06:46:00Z', ")))),
        dict(role="confirmation",case_id="x",turn=1,message=dict(role="assistant",content='{"assessments":[]}'))])
    r=run_role(api,EvidenceSession(db,"stage2",packet),"confirmation","x",dict(bundle=packet))
    assert r["status"]=="completed"
    assert r["trace"][0]["tools"][0]["error_code"]=="invalid_tool_time"
    recorded_delivery(r,"confirmation","x")
    broken=copy.deepcopy(r);broken["trace"][0]["tools"][0]["feedback"]["error"]="changed"
    with pytest.raises(ValueError):recorded_delivery(broken,"confirmation","x")

def test_ranked_five_requires_five_distinct_priorities():
    payload={"bundle":{"a1_role_contract":"simple-v2","root_ranking_policy":"ranked5-v1"}}
    args=dict(decision="resolved",candidates_by_device={},reason="test",missing_evidence=[])
    for i in range(1,6):
        args["candidates_by_device"]["xian-"+["br-1","br-2","service-vm-1","service-vm-2","service-vm-3"][i-1]]=dict(priority=i,confidence=1-i/10,reason="candidate",support=[dict(kind="state",id=str(i),claim="own observation")],counter_citations=[])
    assert len(canonical_result("localization",args,payload)["candidates"])==5
    bad=copy.deepcopy(args);bad["candidates_by_device"].pop("xian-br-1")
    with pytest.raises(ValueError,match="five"):canonical_result("localization",bad,payload)
    bad=copy.deepcopy(args);bad["candidates_by_device"]["xian-br-2"]["priority"]=1
    with pytest.raises(ValueError,match="priorit"):canonical_result("localization",bad,payload)

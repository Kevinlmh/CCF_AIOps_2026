import copy
import pytest
from aiops_v4.agents.review_basis import causal_bases,basis_witnesses,endpoint_associations
from aiops_v4.agents.structured import result_tool,canonical_result,validate_result
from test_evidence_first_roles import encoded,candidates

START="2026-08-24T01:52:00Z";END="2026-08-24T01:54:00Z"
def fixture(root="chengdu-br-1"):
    flow=dict(vector_id="t",entity_id="chengdu-traffic-vm",source="traffic",candidate=True,start_time=START,end_time=END,dimensions=dict(flow_type="web",target_domain="web01.shanghai.aiops.local",target_region="shanghai"),references=[])
    router=dict(vector_id="r",entity_id="chengdu-br-1",source="node",candidate=False,start_time=START,end_time=END,feature_values={"cpu_usage::mean":{"raw_value":.65}})
    server=dict(vector_id="s",entity_id="shanghai-service-vm-1",source="node",candidate=False,start_time=START,end_time=END,feature_values={"cpu_usage::mean":{"raw_value":.39}})
    packet=dict(initial_evidence_profile="a1-readable-1",a1_role_contract="simple-v2",root_ranking_policy="ranked5-v3",endpoint_associations=endpoint_associations([flow]))
    payload=dict(bundle=packet,event=dict(window_ids=["t"],start_time=START,end_time=END),independent_diagnoses=dict(localization=dict(result=dict(candidates=[dict(network_element_id=root)]))))
    return payload,{("state",r["vector_id"]):r for r in [flow,router,server]}

def review(root_id="r",basis="unestablished",root_verdict="insufficient",decision="defer"):
    proof=lambda i:dict(kind="state",id=i,claim="Actual fixture observation")
    return dict(decision=decision,reason="Independent component review",support=[proof("t")],mechanism_support=[],counter_citations=[],missing_evidence=["root mechanism unestablished"] if decision=="defer" else [],counterevidence_assessment="Normal resource values do not prove a network fault",contradictions=[],root_check=dict(verdict=root_verdict,basis=basis,reason="Root and category checked separately",proofs=[proof(root_id)]),category_check=dict(verdict="supported",reason="Observed business symptom",proofs=[proof("t")]))

def test_unrelated_normal_router_cannot_be_accepted_for_business_symptom():
    payload,seen=fixture();assert causal_bases(payload,seen)==set()
    schema=result_tool("review",payload,seen)["function"]["parameters"]
    assert all(o["properties"]["z_decision"]["enum"]!=["accept"] for o in schema["anyOf"])
    bad=review(root_verdict="supported",decision="accept")
    with pytest.raises(ValueError,match="causal basis"):canonical_result("review",encoded(bad),payload,seen)
    out=canonical_result("review",encoded(review()),payload,seen);validate_result("review",out,payload,seen)
    assert out["decision"]=="defer"

def test_actual_target_association_allows_service_root_even_with_normal_cpu():
    payload,seen=fixture("shanghai-service-vm-1");assert causal_bases(payload,seen)=={"mapped_service_target"}
    assert basis_witnesses(payload,seen)["mapped_service_target"]=={("state","s")}
    args=review("s","mapped_service_target","supported","accept")
    out=canonical_result("review",encoded(args),payload,seen);validate_result("review",out,payload,seen);assert out["decision"]=="accept"

def test_actual_router_mechanism_is_allowed_without_forcing_target_server():
    payload,seen=fixture();seen[("state","route")]=dict(vector_id="route",entity_id="chengdu-br-1",source="routing",candidate=True,start_time=START,end_time=END,feature_values={"bgp_peer_up":{"raw_value":0}})
    assert "routing_state_change" in causal_bases(payload,seen)
    args=review("route","routing_state_change","supported","accept");out=canonical_result("review",encoded(args),payload,seen);assert out["decision"]=="accept"
    args["root_check"]["proofs"][0]["id"]="r"
    with pytest.raises(ValueError,match="causal basis"):canonical_result("review",encoded(args),payload,seen)

def test_outside_event_resource_change_is_not_causal_support():
    payload,seen=fixture();seen[("state","r")].update(candidate=True,start_time="2026-08-23T00:00:00Z",end_time="2026-08-23T00:01:00Z")
    assert not causal_bases(payload,seen)

def test_category_correct_cannot_replace_root_review():
    payload,seen=fixture("shanghai-service-vm-1");args=review("s","mapped_service_target","insufficient","accept")
    with pytest.raises(ValueError,match="root AND category"):canonical_result("review",encoded(args),payload,seen)

def test_can_score_every_candidate_then_honestly_abstain():
    rows=candidates();pool={r["network_element_id"]:{k:v for k,v in r.items() if k!="network_element_id"} for r in rows}
    payload=dict(bundle=dict(root_ranking_policy="ranked5-v3",initial_evidence_profile="a1-readable-1",a1_role_contract="simple-v2"))
    args=dict(decision="deferred",candidates_by_device=pool,preferred_device=None,reason="No causal basis despite observed nodes",missing_evidence=["direct mechanism evidence"])
    out=canonical_result("localization",encoded(args),payload);assert out["decision"]=="deferred" and out["candidates"]==[]


def test_component_review_role_and_strict_replay_round_trip():
    from types import SimpleNamespace,MethodType
    import json
    from aiops_v4.agents.tools import EvidenceSession
    from aiops_v4.agents.backend import ReplayBackend
    from aiops_v4.agents.engine import run_role,recorded_delivery
    payload,seen=fixture("shanghai-service-vm-1")
    payload["bundle"].update(candidate_observations=[seen[("state","t")]],root_candidate_context=[seen[("state","r")],seen[("state","s")]],support=[seen[("state","t")]])
    payload["independent_diagnoses"]["localization"]["evidence"]=[dict(kind="state",id="s",data=seen[("state","s")])]
    payload["independent_diagnoses"]["classification"]=dict(result=dict(category=dict(major_category="service",sub_category="web_5xx")),evidence=[dict(kind="state",id="t",data=seen[("state","t")])])
    session=SimpleNamespace(packet=payload["bundle"],seen=dict(seen),anchors={},groups=set(),_chunks={},prepare_candidates=lambda ids:None)
    session._register=MethodType(EvidenceSession._register,session)
    class Structured(ReplayBackend):
        structured_roles=True
        def metadata(self):return dict(super().metadata(),structured_roles=True)
    args=encoded(review("s","mapped_service_target","supported","accept"))
    message=dict(role="assistant",content=None,tool_calls=[dict(id="final",type="function",function=dict(name="submit_role_result",arguments=json.dumps(args)))])
    api=Structured("fixture",[dict(role="review",case_id="fixture",turn=0,message=message)])
    run=run_role(api,session,"review","fixture",payload);assert run["status"]=="completed",run.get("error_code")
    recorded_delivery(run,"review","fixture")

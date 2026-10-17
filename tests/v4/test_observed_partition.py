import copy
import pytest
from aiops_v4.agents.partition import observed_components,component_description
from aiops_v4.agents.structured import canonical_result,validate_result,result_tool
from test_evidence_first_roles import encoded

def windows():
    return [dict(vector_id=k,event_id="native",source="node",entity_id="xian-br-1",start_time=a,end_time=b) for k,a,b in [("a","2026-08-24T00:00:00Z","2026-08-24T00:01:00Z"),("b","2026-08-24T00:01:00Z","2026-08-24T00:02:00Z"),("c","2026-08-24T00:10:00Z","2026-08-24T00:11:00Z")]]

def test_components_retain_all_windows_once_and_do_not_bridge_gaps():
    rows=windows();groups=observed_components(rows);assert [g["window_ids"] for g in groups]==[["a","b"],["c"]]
    assert component_description(rows)["c0"]["window_aliases"]==["w0","w1"]
    with pytest.raises(ValueError,match="duplicate"):observed_components(rows+[rows[0]])

def test_native_component_map_and_canonical_coverage():
    rows=windows();seen={("state",r["vector_id"]):r for r in rows};payload=dict(manifest=rows,bundle=dict(root_ranking_policy="ranked5-v4",a1_role_contract="simple-v2",initial_evidence_profile="a1-readable-1"))
    def item(i):return dict(decision="confirmed",confidence=.8,reason="Fixture",support=[dict(kind="state",id=i,claim="Fixture observation")],mechanism_support=[],counter_citations=[],missing_evidence=[])
    args=encoded(dict(component_assessments={"c0":item("a"),"c1":item("c")}))
    out=canonical_result("confirmation",args,payload);cases=validate_result("confirmation",out,payload,seen)
    assert {i for c in cases for i in c["window_ids"]}=={"a","b","c"} and len(cases)==2
    schema=result_tool("confirmation",payload,seen)["function"]["parameters"]["properties"]["component_assessments"]
    assert schema["required"]==["c0","c1"] and schema["additionalProperties"] is False
    bad=copy.deepcopy(args);bad["component_assessments"].pop("c1")
    with pytest.raises(ValueError,match="component"):canonical_result("confirmation",bad,payload)

def test_contiguous_multiview_windows_are_one_observed_component():
    rows=windows()[:2];rows.append(dict(rows[0],vector_id="parallel",source="traffic"))
    assert len(observed_components(rows))==1


def test_temporal_query_schema_excludes_historical_trailing_quote_error():
    import re
    from aiops_v4.agents.structured import role_tools
    rows=windows();seen={("state",r["vector_id"]):r for r in rows}
    payload=dict(manifest=rows,bundle=dict(root_ranking_policy="ranked5-v4",a1_role_contract="simple-v2",initial_evidence_profile="a1-readable-1"))
    tool=next(t for t in role_tools("confirmation",payload,seen) if t["function"]["name"]=="query_states")
    pattern=tool["function"]["parameters"]["properties"]["start"]["pattern"]
    assert re.fullmatch(pattern,"2026-08-27T06:46:00.000000Z")
    assert re.fullmatch(pattern,"2026-08-27T06:46:00+08:00")
    assert not re.fullmatch(pattern,"2026-08-27T06:46:00.000000Z', ")

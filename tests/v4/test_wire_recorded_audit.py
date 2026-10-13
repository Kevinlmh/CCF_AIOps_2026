import copy
import pytest
from test_agent_tools import evidence
from test_structured_roles import StructuredReplay,fixture_role,good_output
from aiops_v4.agents.engine import recorded_delivery
from aiops_v4.agents.backend import HTTPBackend,Completion
from aiops_v4.agents.jsonio import dumps
from aiops_v4.agents.wire import pack_request,VERSION
import hashlib


def compact_run(evidence,monkeypatch):
    backend=HTTPBackend('fixture-model','http://127.0.0.1:8000/v1','fixture',structured_roles=True,compact_evidence=True,enable_thinking=False,temperature=0,max_completion_tokens=1536)
    original=StructuredReplay.complete
    def metadata(self):return backend.metadata()
    def complete(self,messages,tools,**kwargs):
        body=dict(model=self.model,messages=messages,tools=tools,tool_choice='required',parallel_tool_calls=False,max_completion_tokens=1536,chat_template_kwargs={'enable_thinking':False},temperature=0)
        wire,codec=pack_request(body);encoded=dumps(wire).encode()
        stats=dict(version=VERSION,logical_bytes=len(dumps(body).encode()),aliases=len(codec.aliases),request_bytes=len(encoded),request_sha256=hashlib.sha256(encoded).hexdigest())
        result=original(self,messages,tools,**kwargs)
        return Completion(result.message,result.usage,result.model,stats)
    monkeypatch.setattr(StructuredReplay,'metadata',metadata)
    monkeypatch.setattr(StructuredReplay,'complete',complete)
    return fixture_role(evidence,lambda s,m:[good_output(s,m)])[1]


def test_compact_recorded_http_is_verified(evidence,monkeypatch):
    run=compact_run(evidence,monkeypatch)
    assert run['status']=='completed'
    recorded_delivery(run,'confirmation','case')


@pytest.mark.parametrize('field',['version','request_sha256','request_bytes','logical_bytes','aliases','missing'])
def test_wire_record_tampering_rejected(evidence,monkeypatch,field):
    run=compact_run(evidence,monkeypatch)
    if field=='missing':del run['trace'][0]['wire']
    else:run['trace'][0]['wire'][field]='bad' if field in ('version','request_sha256') else -1
    with pytest.raises(ValueError):recorded_delivery(run,'confirmation','case')


def test_simulated_replay_cannot_claim_compact_http(evidence):
    _,run=fixture_role(evidence,lambda s,m:[good_output(s,m)])
    run['backend']['evidence_wire']=VERSION
    with pytest.raises(ValueError):recorded_delivery(run,'confirmation','case')


def test_unexpected_wire_on_legacy_record_rejected(evidence):
    _,run=fixture_role(evidence,lambda s,m:[good_output(s,m)])
    run['trace'][0]['wire']={'version':VERSION}
    with pytest.raises(ValueError):recorded_delivery(run,'confirmation','case')

@pytest.mark.parametrize('field',['aliases','request_bytes','logical_bytes'])
def test_wire_integer_fields_reject_boolean_and_float(evidence,monkeypatch,field):
    run=compact_run(evidence,monkeypatch)
    for replacement in (float(run['trace'][0]['wire'][field]),True):
        bad=copy.deepcopy(run);bad['trace'][0]['wire'][field]=replacement
        with pytest.raises(ValueError):recorded_delivery(bad,'confirmation','case')

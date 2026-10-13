import json,struct
import pytest
from aiops_v4.agents.wire import pack_request,unpack_request,encode_value,decode_value

def sample():
    return dict(bundle=dict(candidate_observations=[dict(vector_id=str(i).zfill(64),entity_id='xian-service-vm-1',source='node',quality_flags=[],feature_values={'cpu::mean':{'raw_value':v},'other':{'raw_value':None}}) for i,v in enumerate([0.0,-0.0,True,1,1.0,2.5])]))
def test_nested_table_exact_types_and_float_bits():
    value=sample();packed=encode_value(value);back=decode_value(packed)
    assert json.dumps(back,sort_keys=True)==json.dumps(value,sort_keys=True)
    assert struct.pack('>d',back['bundle']['candidate_observations'][1]['feature_values']['cpu::mean']['raw_value'])==struct.pack('>d',-0.0)
    assert type(back['bundle']['candidate_observations'][2]['feature_values']['cpu::mean']['raw_value']) is bool

def test_request_alias_roundtrip_and_response():
    sha='a'*64;msgs=[{'role':'system','content':'facts only'},{'role':'user','content':json.dumps(sample())}];tools=[dict(type='function',function=dict(name='proof',parameters={'enum':[sha]}))]
    wire,codec=pack_request(dict(messages=msgs,tools=tools,model='m'))
    assert unpack_request(wire,codec)==dict(messages=msgs,tools=tools,model='m')
    alias=codec.aliases[sha]
    assert codec.decode_response({'role':'assistant','content':None,'tool_calls':[{'function':{'name':'proof','arguments':json.dumps({'id':alias})},'id':'call-1','type':'function'}]})['tool_calls'][0]['function']['arguments']==json.dumps({'id':sha},separators=(',',':'),ensure_ascii=False)
    assert sha not in json.dumps(wire)

def test_literal_tags_reserved_alias_and_json_chunk_roundtrip():
    value={'$v4_table':1,'rows':[1], 'columns':[], 'constants':[], 'text':'literal @h0 '+('b'*64)}
    request=dict(messages=[dict(role='user',content=json.dumps(value)),dict(role='tool',content=json.dumps(dict(text=json.dumps(sample()),complete=True)))],tools=[])
    packed,codec=pack_request(request);assert unpack_request(packed,codec)==request
    assert codec.decode_string('@@h0')=='@h0'

def test_heterogeneous_fields_and_empty_objects_preserved():
    values=[{},dict(a=1),dict(a=None,b=[]),dict(a=False,b={})]
    assert decode_value(encode_value(values))==values

def test_unknown_reference_rejected():
    _,codec=pack_request(dict(messages=[],tools=[]))
    with pytest.raises(ValueError):codec.decode_string('@hZZZZ')

def test_repetitive_resource_packet_smaller():
    v=sample();v['bundle']['candidate_observations']*=20
    wire,codec=pack_request(dict(messages=[dict(role='user',content=json.dumps(v))],tools=[]))
    assert len(json.dumps(wire))<len(json.dumps(dict(messages=[dict(role='user',content=json.dumps(v))],tools=[])))*.6


def test_http_backend_sends_compact_and_restores_tool_arguments():
    from aiops_v4.agents.backend import HTTPBackend
    from aiops_v4.agents.jsonio import dumps
    sha='a'*64;messages=[dict(role='system',content='judge'),dict(role='user',content=dumps(sample()))]
    tools=[dict(type='function',function=dict(name='proof',parameters={'enum':[sha]}))]
    b=HTTPBackend('m','http://127.0.0.1:8000/v1','local',structured_roles=True,compact_evidence=True)
    expected=dict(model='m',messages=messages,tools=tools,tool_choice='required',parallel_tool_calls=False,max_completion_tokens=4096)
    packed,codec=pack_request(expected)
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return dumps(dict(model='m',usage={},choices=[dict(finish_reason='tool_calls',message=dict(role='assistant',content='',tool_calls=[dict(id='call',type='function',function=dict(name='proof',arguments=dumps(dict(id=codec.aliases[sha]))))]))])).encode()
    class Opener:
        def open(self,request,timeout):
            assert json.loads(request.data)==packed
            return Response()
    b._opener=Opener();result=b.complete(messages,tools,role='confirmation',case_id='case',turn=0)
    assert json.loads(result.message['tool_calls'][0]['function']['arguments'])=={'id':sha}
    assert result.wire['version']=='v4-evidence-wire-1'
    assert result.wire['logical_bytes']>0

def test_compact_flag_strict_and_default_metadata_compatible():
    from aiops_v4.agents.backend import HTTPBackend
    b=HTTPBackend('m','http://127.0.0.1:8000/v1','local')
    assert 'evidence_wire' not in b.metadata()
    with pytest.raises(ValueError):HTTPBackend('m','http://127.0.0.1:8000/v1','local',compact_evidence=True)


def test_nested_generated_tables_roundtrip():
    groups=[dict(group=str(i),observations=[dict(metric=j,value=float(i+j),same='node') for j in range(12)]) for i in range(12)]
    value=dict(groups=groups)
    assert decode_value(encode_value(value))==value
    request=dict(messages=[dict(role='user',content=json.dumps(value))],tools=[])
    wire,codec=pack_request(request);assert unpack_request(wire,codec)==request

@pytest.mark.parametrize('before,after',[('_','_'),('','中文'),('@',''),('@@',''),('(','!'),('','/path')])
def test_reference_substrings_roundtrip(before,after):
    text=before+('a'*64)+after
    _,codec=pack_request(dict(messages=[dict(role='user',content=text)],tools=[]))
    assert codec.decode_string(codec.encode_string(text))==text

@pytest.mark.parametrize('prefix',['\n','\t','\r'])
def test_json_escaped_reference_collected_from_decoded_values(prefix):
    sha='b'*64
    value={'text':'prefix'+prefix+sha}
    request=dict(messages=[dict(role='user',content=json.dumps(value))],tools=[])
    wire,codec=pack_request(request)
    assert sha in codec.aliases
    assert unpack_request(wire,codec)==request
    nested=dict(messages=[dict(role='tool',content=json.dumps(dict(complete=True,text=json.dumps(value))))],tools=[])
    wire,codec=pack_request(nested)
    assert unpack_request(wire,codec)==nested


def test_backend_request_encoding_failure_is_sanitized(monkeypatch):
    from aiops_v4.agents.backend import HTTPBackend,BackendError
    import aiops_v4.agents.wire as module
    b=HTTPBackend('m','http://127.0.0.1:8000/v1','local',structured_roles=True,compact_evidence=True)
    def broken(request):raise ValueError('private request details')
    monkeypatch.setattr(module,'pack_request',broken)
    with pytest.raises(BackendError,match='^invalid_wire_request$'):
        b.complete([dict(role='user',content='facts')],[],role='confirmation',case_id='case',turn=0)

@pytest.mark.parametrize('nested',[False,True])
def test_message_serialization_identity_does_not_collide(nested):
    contents=['{"a": 1}','{"a":1}']
    if nested:contents=[json.dumps(dict(complete=True,text=v)) for v in contents]
    request=dict(messages=[dict(role='user' if not nested else 'tool',content=v) for v in contents],tools=[])
    wire,codec=pack_request(request)
    assert unpack_request(wire,codec)==request
    assert unpack_request(wire,codec)==request

@pytest.mark.parametrize('calls',[{},0,False,''])
def test_backend_falsey_malformed_tool_calls_rejected(calls):
    from aiops_v4.agents.backend import _message,BackendError
    with pytest.raises(BackendError,match='invalid_tool_calls'):
        _message(dict(role='assistant',content='ok',tool_calls=calls))


def test_noncompact_bad_unicode_request_is_sanitized():
    from aiops_v4.agents.backend import HTTPBackend,BackendError
    backend=HTTPBackend('m','http://127.0.0.1:8000/v1','local')
    with pytest.raises(BackendError,match='^invalid_backend_request$'):
        backend.complete([dict(role='user',content='bad '+chr(0xd800))],[],role='confirmation',case_id='case',turn=0)

"""Lossless heterogeneous tables for large multi-city evidence requests."""
import copy,struct
import pytest
from aiops_v4.agents.jsonio import dumps,loads
from aiops_v4.agents.wire import pack_request,unpack_request

V3='v4-evidence-wire-3'
def body():
    rows=[]
    for i in range(16):
        row=dict(vector_id=str(i).zfill(64),source='traffic',entity_id='city-'+str(i%8),
                 features={f'metric{j}':dict(raw_value=float(i+j),unit='count',quality_flags=[]) for j in range(40)},
                 rules={f'metric{j}':dict(minimum=i,maximum=i+100,reset_count=0,missing=None) for j in range(20)})
        if i%2:row['features']['sometimes']={'raw_value':None,'unit':'seconds'}
        if i%3:row['rules']['extra']={'value':False}
        rows.append(row)
    return dict(observations=rows,typed=[False,0,0.0,-0.0,None,{},[]])
def req(value):
    return dict(model='m',messages=[dict(role='system',content='Judge facts'),dict(role='user',content=dumps(value))],tools=[])
def check(original):
    wire,codec=pack_request(original,version=V3)
    assert dumps(unpack_request(wire,codec))==dumps(original)
    assert dumps(unpack_request(wire,codec))==dumps(original)
    return wire,codec
def test_heterogeneous_rows_compress_without_dropping_columns():
    original=req(body());old,_=pack_request(original,version='v4-evidence-wire-2')
    new,_=check(original)
    assert len(dumps(new))<len(dumps(old))*.65
    assert '$v4_sparse_table' in dumps(new)
def test_missing_null_empty_and_numeric_types_stay_exact():
    original=req(body());wire,codec=check(original);restored=loads(unpack_request(wire,codec)['messages'][1]['content'])
    assert 'sometimes' not in restored['observations'][0]['features']
    assert restored['observations'][1]['features']['sometimes']['raw_value'] is None
    assert [type(x) for x in restored['typed'][:4]]==[bool,int,float,float]
    assert struct.pack('>d',restored['typed'][3])==struct.pack('>d',-0.0)
@pytest.mark.parametrize('tag',['$v4_sparse_table','$v4_sparse_literal'])
def test_reserved_markers_are_literal(tag):
    value=body();value['untrusted']={tag:1,'rows':[['bad']],'nested':{'$v4_sparse_table':1}}
    check(req(value))
def test_complete_tool_json_transforms_partial_tool_chunk_stays_text():
    original=req(body())
    original['messages'].extend([dict(role='tool',tool_call_id='x',content=dumps(dict(complete=True,text=dumps(body())))),
                                dict(role='tool',tool_call_id='y',content=dumps(dict(complete=False,text='[{"x":',total_chars=80000)))])
    previous,_=pack_request(original,version='v4-evidence-wire-2');current,_=check(original)
    assert previous['messages'][-1]['content']==current['messages'][-1]['content']
def test_path_types_can_differ_between_rows():
    v=body();v['observations'][0]['features']['metric0']=None
    v['observations'][1]['features']['metric0']={}
    check(req(v))
def test_sparse_wire_mutation_fails_integrity_check():
    original=req(body());wire,codec=pack_request(original,version=V3)
    value=loads(wire['messages'][1]['content'])
    def mutate(v):
        if isinstance(v,dict):
            if v.get('$v4_sparse_table')==1:
                v['rows'][0]['absent']=[-1];return True
            return any(mutate(x) for x in v.values())
        if isinstance(v,list):return any(mutate(x) for x in v)
        return False
    assert mutate(value)
    wire['messages'][1]['content']=dumps(value)
    with pytest.raises(ValueError):unpack_request(wire,codec)

def test_dense_counter_table_does_not_repeat_long_metric_paths():
    original=req({'observations':[dict(vector_id=str(i).zfill(64),rule_inputs={
      'traffic.auth.client_region_abcdefgh.counter_'+str(j):dict(mean=i+.1,last=i+.2,min=i,max=i+2,kind='counter',unit='count',usable=True)
      for j in range(24)}) for i in range(12)]})
    old,_=pack_request(original,version='v4-evidence-wire-2');new,_=check(original)
    assert len(dumps(new))<len(dumps(old))*.8

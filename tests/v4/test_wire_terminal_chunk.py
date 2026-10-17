import pytest
from aiops_v4.agents.wire import pack_request,unpack_request,SHAPE_VERSION
from aiops_v4.agents.jsonio import dumps

def body():
    return dict(observations=[dict(entity_id="city-"+str(i%8),features={str(j):dict(value=float(i+j),unit="count",flags=[]) for j in range(40)},extra=({"x":None} if i%2 else {})) for i in range(16)])

@pytest.mark.parametrize("fragment",['rate_mean":0.36}}]','plain terminal text','','null','42'])
def test_terminal_chunk_remains_exact_text(fragment):
    observations=[dict(entity_id="xian-traffic-vm",source="traffic",metric="cpu",same="long duplicated constant value "+"x"*100,value=i) for i in range(100)]
    request=dict(messages=[dict(role="system",content="facts only"),dict(role="user",content=dumps(body())),dict(role="tool",content=dumps(dict(complete=True,text=fragment)))],tools=[])
    packed,codec=pack_request(request,version=SHAPE_VERSION)
    assert codec.sparse_mode
    assert unpack_request(packed,codec)==request

def test_terminal_fragment_tampering_rejected():
    fragment='rate_mean":0.36}}]'
    request=dict(messages=[dict(role="system",content="facts only"),dict(role="user",content=dumps(body())),dict(role="tool",content=dumps(dict(complete=True,text=fragment)))],tools=[])
    packed,codec=pack_request(request,version=SHAPE_VERSION)
    assert codec.sparse_mode
    packed["messages"][-1]["content"]=dumps(dict(complete=True,text="changed fragment"))
    with pytest.raises(ValueError):unpack_request(packed,codec)

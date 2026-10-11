import importlib.util
import json
import math
import pytest


def test_state_dictionary_codec_preserves_types_and_unknown_structure():
    assert importlib.util.find_spec('aiops_v4.states.codec'), 'state delta codec missing'
    from aiops_v4.states.codec import pack_delta,unpack_delta
    strings={};reverse={}
    def intern(value):
        if value not in strings:
            identifier=len(strings)+1;strings[value]=identifier;reverse[identifier]=value
        return strings[value]
    row={'schema_version':1,'candidate':False,'score':0.0,'missing':None,'signed':-2**80,
         'drivers':[{'feature':'a','raw_value':1.2345678901234567}],
         'unknown':{'Unicode键':'中文','empty':[],'big':2**80}}
    blob=pack_delta(row,intern)
    assert blob.startswith(b'V4D1')
    got=unpack_delta(blob,reverse)
    assert got==row and type(got['candidate']) is bool and type(got['score']) is float
    with pytest.raises(ValueError):pack_delta({'score':math.inf},intern)
    with pytest.raises(ValueError):unpack_delta(blob+b'bad',reverse)

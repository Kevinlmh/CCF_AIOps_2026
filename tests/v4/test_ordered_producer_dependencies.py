import pytest
from aiops_v4.data import cache

@pytest.mark.parametrize('layer,relative',[('features','features/flow_codec.py'),('features','features/write_codec.py'),('evidence','states/codec.py'),('evidence','states/matrix.py'),('states','features/write_codec.py'),('evidence','features/write_codec.py')])
def test_helper_changes_invalidate_dependent_layer_signature(monkeypatch,layer,relative):
    original=cache.sha256
    before=cache.producer_hash(layer)
    def changed(path,end=None):
        if str(path).endswith('/aiops_v4/'+relative):return 'f'*64
        return original(path,end)
    monkeypatch.setattr(cache,'sha256',changed)
    assert cache.producer_hash(layer)!=before

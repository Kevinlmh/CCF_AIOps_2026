import math
import pytest
from state_helpers import state_module, window


def test_selects_counter_rates_interval_sums_and_categories_without_unknown():
    # Using raw cumulative values or category means would break these literals.
    w=window(); template=w['metrics']['cpu_usage']
    w['metrics'].update({
        'requests_total':{**template,'semantics':{'kind':'counter','unit':'count','status':'inferred'},'mean':900,'rate_mean':2},
        'bytes':{**template,'semantics':{'kind':'interval_count','unit':'bytes','status':'inferred'},'mean':50,'sum':100},
        'state':{**template,'semantics':{'kind':'gauge','unit':'state_code','status':'inferred'},'mean':2.5,'last':3},
        'mystery':{**template,'semantics':{'kind':'unknown','unit':None,'status':'unverified'}},
    })
    row=state_module('matrix').window_vector(w,'stage2')
    assert row['features']['cpu_usage::mean']['value']==10
    assert row['features']['requests_total::rate_mean']['raw_value']==2
    assert row['features']['requests_total::rate_mean']['value']==pytest.approx(math.log(3))
    assert row['features']['bytes::sum']['raw_value']==100
    assert row['features']['state::last']['value']=='3'
    assert row['features']['state::last']['kind']=='category'
    assert 'mystery' in row['excluded_metrics']


def test_bad_missing_and_broken_counter_cells_remain_masked():
    w=window(); m=w['metrics']['cpu_usage']; m['mean']=None
    w['metrics']['requests_total']={**m,'mean':100,'rate_mean':4,'counter_gap_count':1,
                                    'semantics':{'kind':'counter','unit':'count','status':'inferred'}}
    w['metrics']['load1']={**m,'mean':1,'invalid_input_count':1,
                          'semantics':{'kind':'gauge','unit':'load','status':'inferred'}}
    row=state_module('matrix').window_vector(w,'stage2')
    assert all(cell['value'] is None for cell in row['features'].values())
    assert row['references'][0]['record_id']=='r0'


def test_group_preserves_peer_interface_entity_and_auxiliary_identity():
    fn=state_module('matrix').window_vector
    w=window(source='routing',dimensions={'metric_name':'cpu_usage','peer':'1','label':'peer="1"'})
    peer=window(source='routing',dimensions={'metric_name':'cpu_usage','peer':'2','label':'peer="2"'})
    aux=window(identity={'entity_id':'aux:xian:probe-vm','network_element_id':None,'candidate':False,'city':'xian','role':'probe-vm'})
    assert len({fn(w,'stage2')['group_id'],fn(peer,'stage2')['group_id'],fn(aux,'stage2')['group_id']})==3
    assert fn(aux,'stage2')['identity']['candidate'] is False
    assert fn(window(source='interface',dimensions={'interface_id':'eth0'}),'stage2')['group_id']!=fn(window(source='interface',dimensions={'interface_id':'eth1'}),'stage2')['group_id']


def test_identity_conflict_blocks_learning_without_discarding_evidence():
    row=state_module('matrix').window_vector(window(quality_flags=['city_conflict']),'stage2')
    assert row['quality']['blocked'] is True
    assert row['features']['cpu_usage::mean']['value'] is None
    assert row['references']


@pytest.mark.parametrize('changes',[
    {'batch':'stage1'},{'start_time':'2026-09-17T04:00:00'},
    {'end_time':'2026-09-17T04:00:00Z'},
])
def test_invalid_batch_or_window_time_is_rejected(changes):
    with pytest.raises(ValueError):state_module('matrix').window_vector(window(**changes),'stage2')

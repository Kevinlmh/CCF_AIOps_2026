import pytest
from state_helpers import state_module, window


def vectors(values, *, unit='percent'):
    fn=state_module('matrix').window_vector
    rows=[]
    for i,value in enumerate(values):
        w=window(i,value=value);w['metrics']['cpu_usage']['semantics']['unit']=unit
        rows.append(fn(w,'stage2'))
    return rows


def test_robust_location_scale_and_unclipped_statistical_deviation():
    ref=state_module('reference'); model=ref.fit_reference(vectors([0,1,2,3,4]),min_reference=3)
    feature=model['features']['cpu_usage::mean']
    assert feature['median']==2 and feature['scale']==1
    result=ref.normalize(vectors([100])[0],model)
    assert result['stat_score']==98
    assert result['coverage']==1


def test_constant_reference_does_not_divide_by_zero_or_hide_change():
    ref=state_module('reference'); model=ref.fit_reference(vectors([0]*12))
    assert model['features']['cpu_usage::mean']['scale_basis']=='constant_floor'
    assert ref.normalize(vectors([0])[0],model)['stat_score']==0
    assert ref.normalize(vectors([1])[0],model)['stat_score']==1_000_000


def test_short_reference_is_explicitly_unscorable():
    ref=state_module('reference'); model=ref.fit_reference(vectors([1,2]))
    assert model['status']=='insufficient_reference'
    assert ref.normalize(vectors([100])[0],model)['stat_score'] is None


def test_categorical_reference_detects_new_value_without_state_code_mean():
    ref=state_module('reference');model=ref.fit_reference(vectors([2]*12,unit='state_code'))
    result=ref.normalize(vectors([3],unit='state_code')[0],model)
    assert result['unseen_categories']==['cpu_usage::last']
    assert result['stat_score'] is None
    assert result['values']=={'cpu_usage::last':'3'}


def test_reference_reservoirs_are_bounded_but_counts_cover_all_points():
    model=state_module('reference').fit_reference(vectors(range(1000)),min_reference=4,capacity=8)
    assert model['features']['cpu_usage::mean']['observations']==1000
    assert model['features']['cpu_usage::mean']['sample_count']==8
    assert len(model['_sample_rows'])<=64


def test_mixed_distance_uses_only_sufficient_common_cells():
    fn=state_module('cluster').distance
    assert fn({'x':0,'y':None},{'x':2,'y':3},minimum_overlap=0.5)==2
    assert fn({'x':0,'y':None},{'x':None,'y':3},minimum_overlap=0.5) is None
    assert fn({'state':'1'},{'state':'2'},minimum_overlap=0.5)==1
    assert fn({'x':1000},{'x':0},minimum_overlap=0.5)==20


def test_medoids_recover_two_states_and_avoid_duplicate_centers():
    cluster=state_module('cluster')
    points=[{'x':0},{'x':0},{'x':10},{'x':10}]
    model=cluster.fit_clusters(points,k=3)
    assert len(model['medoids'])==2
    assert sorted(m['values']['x'] for m in model['medoids'])==[0,10]
    assert sorted(m['support'] for m in model['medoids'])==[0.5,0.5]
    assert cluster.assign({'x':0},model)['distance']==0
    assert cluster.fit_clusters(points,k=3)==model


def test_disjoint_point_remains_unassigned():
    cluster=state_module('cluster')
    model=cluster.fit_clusters([{'x':0,'y':None}]*12)
    result=cluster.assign({'x':None,'y':10},model)
    assert result['cluster_id'] is None and result['distance'] is None


@pytest.mark.parametrize('kwargs',[{'k':0},{'minimum_overlap':float('nan')},{'iterations':0}])
def test_invalid_clustering_budget_is_rejected(kwargs):
    with pytest.raises(ValueError):state_module('cluster').fit_clusters([{'x':0}],**kwargs)

"""Keep all k-medoids samples while avoiding repeated mixed distance work."""
import json
import math
import os
import subprocess
import sys
import pytest
from aiops_v4.states import cluster


def reference_distance(left,right,overlap,stable=False):
    keys=left.keys()|right.keys();costs=[]
    for name in sorted(keys) if stable else keys:
        a,b=left.get(name),right.get(name)
        if a is None or b is None:continue
        if isinstance(a,str) and isinstance(b,str):costs.append(float(a!=b))
        elif isinstance(a,(int,float)) and isinstance(b,(int,float)) and math.isfinite(a) and math.isfinite(b):
            difference=max(-20,min(20,a))-max(-20,min(20,b));costs.append(difference*difference)
        else:raise ValueError('incompatible feature value types')
    if not keys or not costs or len(costs)/len(keys)<overlap:return None
    return math.sqrt((math.fsum(costs) if stable else sum(costs))/len(costs))


def reference_fit(points,k=3,overlap=.5,iterations=6,stable=False):
    # The original uncached algorithm, including duplicate samples and index ties.
    keys=sorted({name for point in points for name in point})
    points=[{name:point.get(name) for name in keys} for point in points]
    pairs=[[reference_distance(a,b,overlap,stable) for b in points] for a in points]
    seeds=[i for i in range(len(points)) if pairs[i][i] is not None];medoids=seeds[:1]
    while medoids and len(medoids)<k:
        candidates=[]
        for i in seeds:
            ds=[pairs[i][m] for m in medoids if pairs[i][m] is not None]
            if ds:candidates.append((min(ds),-i,i))
        if not candidates or max(candidates)[0]==0:break
        medoids.append(max(candidates)[2])
    for _ in range(iterations):
        members={m:[] for m in medoids}
        for i in seeds:
            ds=[(pairs[i][m],j,m) for j,m in enumerate(medoids) if pairs[i][m] is not None]
            if ds:members[min(ds)[2]].append(i)
        updated=[]
        for medoid in medoids:
            candidates=members[medoid]
            if not candidates:continue
            costs=[(sum(pairs[i][j] if pairs[i][j] is not None else math.inf for j in candidates),i) for i in candidates]
            chosen=min(costs)[1]
            if chosen not in updated:updated.append(chosen)
        if updated==medoids:break
        medoids=updated
    result=dict(algorithm='k-medoids_mixed_masked',feature_names=keys,medoids=[],reference_sample_count=len(points),
        unassigned_reference_count=0,minimum_overlap=overlap,distance_numeric_clip=20)
    result['medoids']=[dict(cluster_id=i,values=points[m],support=0.) for i,m in enumerate(medoids)]
    counts=[0]*len(medoids)
    for point in points:
        candidates=[]
        for medoid in result['medoids']:
            distance=reference_distance(point,medoid['values'],overlap,stable)
            if distance is not None:candidates.append((distance,medoid['cluster_id'],medoid['support']))
        if candidates:counts[min(candidates)[1]]+=1
        else:result['unassigned_reference_count']+=1
    for medoid,count in zip(result['medoids'],counts):medoid['support']=count/len(points)
    return result


def same(left,right):
    assert type(left) is type(right),(left,right)
    if isinstance(left,dict):
        assert left.keys()==right.keys()
        for key in left:same(left[key],right[key])
    elif isinstance(left,list):
        assert len(left)==len(right)
        for a,b in zip(left,right):same(a,b)
    elif isinstance(left,float):assert math.isclose(left,right,rel_tol=0,abs_tol=1e-12),(left,right)
    else:assert left==right


def test_mixed_distances_are_identical_across_python_hash_seeds():
    left={f'f{i:03d}':0. for i in range(128)}
    right={name:(20. if name=='f000' else 1e-7) for name in left}
    payload=json.dumps([left,right])
    script="""import json,sys
from aiops_v4.states.cluster import distance,fit_clusters,assign
left,right=json.loads(sys.argv[1]);model=fit_clusters([left,left,right,right],k=3)
probe={name:value*.5 for name,value in right.items()}
print(json.dumps(dict(distance=distance(left,right),model=model,assignment=assign(probe,model)),sort_keys=True))
"""
    outputs=[]
    for seed in ('0','1','2','3','7','31','97','128'):
        process=subprocess.run([sys.executable,'-c',script,payload],env=dict(os.environ,PYTHONHASHSEED=seed),
            text=True,capture_output=True,check=True)
        outputs.append(process.stdout)
    assert len(set(outputs))==1,[json.loads(value)['distance'].hex() for value in outputs]


def test_exact_duplicate_samples_share_symmetric_distances_without_losing_support(monkeypatch):
    points=[{'x':0.,'state':'1','masked':None}]*32+[{'x':10.,'state':'2','masked':None}]*32
    original=cluster.distance;calls=[]
    def distance(left,right,minimum_overlap=.5):
        calls.append((left,right));return original(left,right,minimum_overlap)
    monkeypatch.setattr(cluster,'distance',distance)
    result=cluster.fit_clusters(points,k=3)
    same(reference_fit(points),result)
    assert len(calls)==3,len(calls)  # Two unique points: two self pairs and one cross pair.
    assert result['reference_sample_count']==64 and result['unassigned_reference_count']==0
    assert [m['support'] for m in result['medoids']]==[.5,.5]


@pytest.mark.parametrize('points,k,overlap',[
    ([{'x':0.,'state':'a','masked':None},{'x':1.,'state':'a'},{'x':10.,'state':'b','masked':None}]*3,3,.5),
    ([{'x':-1000.,'state':'a'},{'x':-20.,'state':'a'},{'x':20.,'state':'b'},{'x':1000.,'state':'b'}],3,.5),
    ([{'x':None},{'x':None}]*4,3,.5),
    ([{},{}],3,.5),
    ([{'x':0.,'y':None},{'x':None,'y':2.}],3,1.),
    ([{'x':4.,'state':'a'}]*64,8,.5),
    ([{'x':0.},{'x':2.},{'x':4.},{'x':6.},{'x':8.},{'x':10.}],3,.5),
    ([{'x':0.,'y':1e-12},{'x':2.,'y':0.},{'x':4.,'y':-1e-12},{'x':6.,'y':0.}],3,.5),
    ([{'x':0},{'x':0.},{'x':False},{'x':-0.},{'x':1},{'x':True}],3,.5),
])
def test_cached_models_preserve_uncached_medoids_masks_counts_and_index_ties(points,k,overlap):
    result=cluster.fit_clusters(points,k=k,minimum_overlap=overlap)
    same(reference_fit(points,k,overlap),result)
    same(reference_fit(points,k,overlap,stable=True),result)


def test_type_and_float_bit_patterns_do_not_merge_distinct_reference_samples(monkeypatch):
    points=[{'x':0},{'x':0.},{'x':False},{'x':-0.}]*4
    original=cluster.distance;calls=[]
    def distance(left,right,minimum_overlap=.5):
        calls.append((left,right));return original(left,right,minimum_overlap)
    monkeypatch.setattr(cluster,'distance',distance)
    result=cluster.fit_clusters(points,k=3)
    assert len(calls)==10,len(calls)
    assert type(result['medoids'][0]['values']['x']) is int
    assert result['medoids'][0]['support']==1 and result['reference_sample_count']==16


@pytest.mark.parametrize('left,right,overlap',[
    ({'x':1.,'y':None,'s':'a'},{'x':3.,'y':8.,'s':'b'},.5),
    ({'x':1e300,'y':1e-7},{'x':-1e300,'y':0.},1.),
    ({f'f{i}':0. for i in range(128)},{f'f{i}':20. if i==0 else 1e-7 for i in range(128)},.5),
    ({'x':None},{'x':1.},.5),
])
def test_stable_distance_stays_within_previous_rounding_bound(left,right,overlap):
    old=reference_distance(left,right,overlap);new=cluster.distance(left,right,overlap)
    if old is None:assert new is None
    else:assert abs(old-new)<=1e-12


@pytest.mark.parametrize('distinct',[1,64])
def test_distance_work_is_bounded_by_unique_symmetric_pairs(monkeypatch,distinct):
    points=[{'x':float(i%distinct),'state':str(i%3 if distinct>1 else 0)} for i in range(64)]
    original=cluster.distance;calls=[]
    def distance(left,right,minimum_overlap=.5):
        calls.append((left,right));return original(left,right,minimum_overlap)
    monkeypatch.setattr(cluster,'distance',distance)
    model=cluster.fit_clusters(points,k=3)
    assert len(calls)==distinct*(distinct+1)//2
    assert model['reference_sample_count']==64
    same(reference_fit(points,stable=True),model)


@pytest.mark.parametrize('points',[[{'x':'a'},{'x':1.}],[{'x':float('nan')}],[{'x':float('inf')}],[{'x':[]} ]])
def test_cache_retains_mixed_value_validation(points):
    with pytest.raises(ValueError,match='incompatible feature value types'):cluster.fit_clusters(points)

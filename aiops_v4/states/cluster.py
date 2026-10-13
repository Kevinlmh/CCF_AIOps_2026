"""Deterministic bounded k-medoids with explicit mixed-feature masks."""
import math
import struct


def _overlap(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError('minimum_overlap must be finite in (0,1]')


def distance(left, right, minimum_overlap=0.5):
    _overlap(minimum_overlap)
    keys = sorted(left.keys() | right.keys())
    costs = []
    for name in keys:
        a, b = left.get(name), right.get(name)
        if a is None or b is None:
            continue
        if isinstance(a, str) and isinstance(b, str):
            costs.append(float(a != b))
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and math.isfinite(a) and math.isfinite(b):
            difference = max(-20, min(20, a)) - max(-20, min(20, b))
            costs.append(difference * difference)
        else:
            raise ValueError('incompatible feature value types')
    if not keys or not costs or len(costs) / len(keys) < minimum_overlap:
        return None
    return math.sqrt(math.fsum(costs) / len(costs))



def _point_key(point):
    """Only reuse exact scalar values with the same type and float bits."""
    result=[]
    for value in point.values():
        kind=type(value)
        if kind is float:result.append((kind,struct.pack('<d',value)))
        elif kind in (str,int,bool,type(None)):result.append((kind,value))
        else:return None  # Keep distance()'s original validation for other types.
    return tuple(result)


def _reference_pairs(points,minimum_overlap):
    # Duplicate samples stay in the full matrix and in all medoid/count loops.
    # The local dictionary only removes repeated symmetric distance work.
    unique=[];identifiers=[];seen={}
    for point in points:
        key=_point_key(point)
        if key is None or key not in seen:
            identifier=len(unique);unique.append(point)
            if key is not None:seen[key]=identifier
        else:identifier=seen[key]
        identifiers.append(identifier)
    pairs=[[None]*len(unique) for _ in unique]
    for i,left in enumerate(unique):
        for j in range(i+1):
            value=distance(left,unique[j],minimum_overlap)
            pairs[i][j]=pairs[j][i]=value
    return [[pairs[i][j] for j in identifiers] for i in identifiers]


def fit_clusters(points, k=3, minimum_overlap=0.5, iterations=6) -> dict:
    _overlap(minimum_overlap)
    if type(k) is not int or not 1 <= k <= 8 or type(iterations) is not int or not 1 <= iterations <= 20:
        raise ValueError('k must be 1..8 and iterations 1..20')
    if len(points) > 64:
        raise ValueError('clustering reference exceeds 64-point budget')
    keys = sorted({key for point in points for key in point})
    points = [{key: point.get(key) for key in keys} for point in points]
    pairs = _reference_pairs(points, minimum_overlap)
    seeds = [i for i in range(len(points)) if pairs[i][i] is not None]
    medoids = seeds[:1]
    while medoids and len(medoids) < k:
        candidates = []
        for i in seeds:
            ds = [pairs[i][m] for m in medoids if pairs[i][m] is not None]
            if ds:
                candidates.append((min(ds), -i, i))
        if not candidates or max(candidates)[0] == 0:
            break
        medoids.append(max(candidates)[2])
    for _ in range(iterations):
        members = {m: [] for m in medoids}
        for i in seeds:
            ds = [(pairs[i][m], j, m) for j, m in enumerate(medoids) if pairs[i][m] is not None]
            if ds:
                members[min(ds)[2]].append(i)
        updated = []
        for medoid in medoids:
            candidates = members[medoid]
            if not candidates:
                continue
            costs = [(sum(pairs[i][j] if pairs[i][j] is not None else math.inf for j in candidates), i)
                     for i in candidates]
            chosen = min(costs)[1]
            if chosen not in updated:
                updated.append(chosen)
        if updated == medoids:
            break
        medoids = updated
    result = {'algorithm': 'k-medoids_mixed_masked', 'feature_names': keys, 'medoids': [],
              'reference_sample_count': len(points), 'unassigned_reference_count': 0,
              'minimum_overlap': minimum_overlap, 'distance_numeric_clip': 20}
    result['medoids'] = [{'cluster_id': i, 'values': points[m], 'support': 0.0} for i, m in enumerate(medoids)]
    counts = [0] * len(medoids)
    for i in range(len(points)):
        candidates = [(pairs[i][m], j) for j, m in enumerate(medoids) if pairs[i][m] is not None]
        if not candidates:
            result['unassigned_reference_count'] += 1
        else:
            counts[min(candidates)[1]] += 1
    for medoid, count in zip(result['medoids'], counts):
        medoid['support'] = count / len(points)
    return result


def assign(values, clusters, minimum_overlap=0.5) -> dict:
    _overlap(minimum_overlap)
    values = {name: values.get(name) for name in clusters['feature_names']}
    candidates = []
    for medoid in clusters['medoids']:
        d = distance(values, medoid['values'], minimum_overlap)
        if d is not None:
            candidates.append((d, medoid['cluster_id'], medoid['support']))
    if not candidates:
        return {'cluster_id': None, 'distance': None, 'support': None}
    d, cluster_id, support = min(candidates)
    return {'cluster_id': cluster_id, 'distance': d, 'support': support}

"""Deterministic bounded k-medoids with explicit mixed-feature masks."""
import math


def _overlap(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError('minimum_overlap must be finite in (0,1]')


def distance(left, right, minimum_overlap=0.5):
    _overlap(minimum_overlap)
    keys = left.keys() | right.keys()
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
    return math.sqrt(sum(costs) / len(costs))


def fit_clusters(points, k=3, minimum_overlap=0.5, iterations=6) -> dict:
    _overlap(minimum_overlap)
    if type(k) is not int or not 1 <= k <= 8 or type(iterations) is not int or not 1 <= iterations <= 20:
        raise ValueError('k must be 1..8 and iterations 1..20')
    if len(points) > 64:
        raise ValueError('clustering reference exceeds 64-point budget')
    keys = sorted({key for point in points for key in point})
    points = [{key: point.get(key) for key in keys} for point in points]
    pairs = [[distance(a, b, minimum_overlap) for b in points] for a in points]
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
    for point in points:
        assignment = assign(point, result, minimum_overlap)
        if assignment['cluster_id'] is None:
            result['unassigned_reference_count'] += 1
        else:
            counts[assignment['cluster_id']] += 1
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

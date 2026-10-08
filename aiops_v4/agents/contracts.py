"""Deterministic coverage, citation and diagnosis contracts around LLM judgments."""
import hashlib
import math
from aiops_challenge_2026.schema import parse_utc
from aiops_v4.evidence.index import read_index
from .jsonio import dumps


def fields(row, expected):
    if not isinstance(row, dict) or set(row) != set(expected):
        raise ValueError('output fields mismatch')


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('nonempty explanation required')


def confidence(value):
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('confidence must be finite in [0,1]')


def missing(value, deferred=False):
    if not isinstance(value, list):
        raise ValueError('missing_evidence must be list')
    for item in value:
        text(item)
    if deferred and not value:
        raise ValueError('deferred result must name missing evidence')


def citations(value, seen):
    if not isinstance(value, list):
        raise ValueError('citations must be list')
    found = []
    for item in value:
        fields(item, ('kind', 'id', 'stance', 'claim'))
        if item['kind'] not in {'state', 'raw', 'event', 'relation'} or item['stance'] not in {'support', 'counter', 'context'}:
            raise ValueError('unknown citation kind/stance')
        text(item['id']); text(item['claim'])
        key = (item['kind'], item['id'])
        if key not in seen:
            raise ValueError('citation not actually delivered to this role')
        found.append((item, seen[key]))
    return found


def supports_event(found, window_ids, seen):
    selected = set(window_ids)
    for item, row in found:
        if item['stance'] != 'support':
            continue
        if item['kind'] == 'state' and item['id'] in selected:
            return True
        if item['kind'] == 'raw':
            for window in selected:
                state = seen.get(('state', window), {})
                if any(ref['record_id'] == item['id'] for ref in state.get('references', [])):
                    return True
    return False


def load_manifest(database, batch, bundle_id, max_windows=2000):
    if type(max_windows) is not int or max_windows < 1:
        raise ValueError('positive manifest budget required')
    with read_index(database, batch) as db:
        if db.execute('SELECT 1 FROM bundles WHERE id=?', (bundle_id,)).fetchone() is None:
            raise ValueError('unknown bundle')
        rows = db.execute('''SELECT s.id,w.event,s.source,s.grp,s.entity,s.start,s.end FROM states s
              JOIN event_windows w ON w.id=s.id JOIN members m ON m.event=w.event
              WHERE m.bundle=? ORDER BY s.start,s.end,s.id LIMIT ?''', (bundle_id, max_windows + 1)).fetchall()
    if len(rows) > max_windows:
        raise ValueError('manifest_budget')
    return [dict(zip(('vector_id', 'event_id', 'source', 'group_id', 'entity_id', 'start_time', 'end_time'), row)) for row in rows]


def validate_confirmation(output, manifest, seen):
    fields(output, ('assessments',))
    if not isinstance(output['assessments'], list):
        raise ValueError('assessments must be list')
    windows = {w['vector_id']: w for w in manifest}
    if len(windows) != len(manifest):
        raise ValueError('duplicate manifest window')
    allocated, result = set(), []
    for item in output['assessments']:
        fields(item, ('decision', 'window_ids', 'confidence', 'reason', 'citations', 'missing_evidence'))
        if item['decision'] not in {'confirmed', 'rejected', 'deferred'}:
            raise ValueError('invalid confirmation decision')
        ids = item['window_ids']
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise ValueError('nonempty window_ids required')
        selected = set(ids)
        if len(selected) != len(ids) or not selected <= windows.keys() or selected & allocated:
            raise ValueError('unknown or duplicate window assignment')
        confidence(item['confidence']); text(item['reason'])
        missing(item['missing_evidence'], item['decision'] == 'deferred')
        found = citations(item['citations'], seen)
        ordered = sorted((windows[i] for i in ids), key=lambda w: (parse_utc(w['start_time']), parse_utc(w['end_time'])))
        cursor = parse_utc(ordered[0]['end_time'])
        for window in ordered:
            start, end = parse_utc(window['start_time']), parse_utc(window['end_time'])
            if start >= end or start > cursor:
                raise ValueError('event windows must form a continuous observed interval')
            cursor = max(cursor, end)
        if item['decision'] == 'confirmed' and not supports_event(found, ids, seen):
            raise ValueError('confirmed event requires observed support from selected windows')
        start = ordered[0]['start_time']
        end = max(ordered, key=lambda w: parse_utc(w['end_time']))['end_time']
        identifier = 'v4-event-' + hashlib.sha256(dumps(dict(windows=sorted(ids), start=start, end=end)).encode()).hexdigest()[:32]
        result.append(dict(item, window_ids=sorted(ids), event_id=identifier,
                           native_event_ids=sorted({windows[i]['event_id'] for i in ids}), start_time=start, end_time=end,
                           boundary_basis='observed_candidate_windows; physical_onset_unknown'))
        allocated.update(selected)
    if allocated != windows.keys():
        raise ValueError('all candidate windows must be assigned exactly once')
    return result


def validate_localization(output, seen):
    from aiops_challenge_2026.schema import VALID_NETWORK_ELEMENTS
    fields(output, ('decision', 'candidates', 'reason', 'missing_evidence'))
    if output['decision'] not in {'resolved', 'deferred'} or not isinstance(output['candidates'], list):
        raise ValueError('invalid localization decision/candidates')
    text(output['reason']); missing(output['missing_evidence'], output['decision'] == 'deferred')
    if bool(output['candidates']) != (output['decision'] == 'resolved'):
        raise ValueError('resolved requires candidates; deferred must abstain')
    devices = set()
    for candidate in output['candidates']:
        fields(candidate, ('network_element_id', 'confidence', 'reason', 'citations'))
        node = candidate['network_element_id']
        if not isinstance(node, str) or node not in VALID_NETWORK_ELEMENTS or node in devices:
            raise ValueError('root device must be official and unique')
        confidence(candidate['confidence']); text(candidate['reason'])
        found = citations(candidate['citations'], seen)
        if not any(item['stance'] == 'support' and item['kind'] in {'state', 'raw'} and row.get('entity_id') == node for item, row in found):
            raise ValueError('each root device needs its own observed support')
        devices.add(node)
    return output


def validate_classification(output, event, seen):
    from aiops_challenge_2026.schema import VALID_MAJOR_SUB_PAIRS
    fields(output, ('decision', 'category', 'confidence', 'reason', 'citations', 'missing_evidence'))
    if output['decision'] not in {'resolved', 'deferred'}:
        raise ValueError('invalid classification decision')
    confidence(output['confidence']); text(output['reason'])
    missing(output['missing_evidence'], output['decision'] == 'deferred')
    found = citations(output['citations'], seen)
    if output['decision'] == 'resolved':
        fields(output['category'], ('major_category', 'sub_category'))
        pair = (output['category']['major_category'], output['category']['sub_category'])
        if not all(isinstance(v, str) for v in pair) or pair not in VALID_MAJOR_SUB_PAIRS:
            raise ValueError('category must be an official major/sub pair')
        if not supports_event(found, event['window_ids'], seen):
            raise ValueError('classification needs support from this confirmed event')
    elif output['category'] is not None:
        raise ValueError('deferred classification must abstain with category=null')
    return output

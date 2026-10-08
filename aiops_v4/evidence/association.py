"""Streaming, bounded associations; overlapping observations are not diagnoses."""
import hashlib
from pathlib import Path
from aiops_challenge_2026.config import load_public_config
from aiops_v4.data.reader import utc_time
from aiops_v4.states.pipeline import _load


def iter_bundles(ordered_events, batch):
    """Consume canonical entity/start/end/id order. Full membership belongs in SQL."""
    current, previous = None, None
    for event in ordered_events:
        entity = event['identity']['entity_id']
        start = utc_time(event['start_time'], require_timezone=True)[0]
        end = utc_time(event['end_time'], require_timezone=True)[0]
        key = entity, start, end, event['event_id']
        if event['batch'] != batch or not entity or start >= end or (previous is not None and key < previous):
            raise ValueError('event batch, interval or canonical order mismatch')
        previous = key
        if current is not None and (entity != current['entity_id'] or start >= current['end_time']):
            yield current
            current = None
        if current is None:
            identity = f'{batch}|{entity}|{start}|{event["event_id"]}'
            current = dict(schema_version=1, bundle_id=hashlib.sha256(identity.encode()).hexdigest(),
                           batch=batch, entity_id=entity, identity=event['identity'], start_time=start, end_time=end,
                           association='same_entity_strict_overlap', needs_confirmation=True,
                           event_count=0, event_ids=[], event_ids_truncated=False,
                           references=[], references_truncated=False)
        current['end_time'] = max(end, current['end_time'])
        current['event_count'] += 1
        if len(current['event_ids']) < 32:
            current['event_ids'].append(event['event_id'])
        else:
            current['event_ids_truncated'] = True
        current['references_truncated'] |= event.get('references_truncated', False)
        ids = {r['record_id'] for r in current['references']}
        for ref in event.get('references', []):
            if ref['record_id'] in ids:
                continue
            if len(current['references']) < 32:
                current['references'].append(ref)
                ids.add(ref['record_id'])
            else:
                current['references_truncated'] = True
    if current is not None:
        yield current


def load_topology(path):
    if path is None:
        return {'available': False, 'provenance': None, 'edges': []}
    content = _load(Path(path).read_bytes())
    provenance, edges = content.get('provenance'), content.get('edges')
    if not isinstance(provenance, str) or not provenance.strip() or not isinstance(edges, list) or len(edges) > 512:
        raise ValueError('topology requires nonempty provenance and at most 512 edges')
    config = load_public_config('network_elements')
    legal = {f'{city}-{role}' for city in config['cities'] for role in config['device_roles']}
    seen = set()
    for edge in edges:
        a, b, relation = edge.get('a'), edge.get('b'), edge.get('relation')
        if a not in legal or b not in legal or a == b or not isinstance(relation, str) or not relation.strip():
            raise ValueError('invalid topology endpoints or relation')
        seen.add((*sorted((a, b)), relation.strip()))
    return {'available': True, 'provenance': provenance.strip(),
            'edges': [dict(a=a, b=b, relation=r) for a, b, r in sorted(seen)]}

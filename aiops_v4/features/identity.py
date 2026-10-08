"""Keep auxiliary observations without fabricating a legal root-cause candidate."""

import hashlib
import json

from aiops_challenge_2026.config import load_public_config
from aiops_v4.data.reader import is_missing

_CONFIG = load_public_config('network_elements')
_CANDIDATES = frozenset(f'{city}-{role}' for city in _CONFIG['cities'] for role in _CONFIG['device_roles'])


def entity_identity(record) -> dict:
    if record.node_id in _CANDIDATES:
        return {'entity_id': record.node_id, 'network_element_id': record.node_id,
                'candidate': True, 'city': record.city, 'role': record.node_id.split('-', 1)[1]}
    fields = ('hostname', 'node', 'node_key') if record.source == 'frr' else ('node_key', 'node', 'hostname')
    raw_name = next((record.raw[name].strip() for name in fields if not is_missing(record.raw.get(name))), None)
    raw_region = next((record.raw[name].strip() for name in ('region_code', 'region', 'source_region')
                       if not is_missing(record.raw.get(name))), 'unknown')
    city = record.city or raw_region
    if raw_name:
        entity_id, role = f'aux:{city}:{raw_name}', raw_name
    else:
        details = [record.source, city, record.source_file, record.raw.get('source_ip'), record.raw.get('target_id')]
        entity_id = 'unmapped:' + hashlib.sha256(json.dumps(details, ensure_ascii=False).encode()).hexdigest()[:24]
        role = 'unknown'
    return {'entity_id': entity_id, 'network_element_id': None, 'candidate': False,
            'city': record.city, 'role': role}

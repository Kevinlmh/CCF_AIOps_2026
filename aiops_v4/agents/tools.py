"""Read-only batch tools with explicit chunks and delivered-evidence tracking."""
import hashlib
from .jsonio import dumps
from aiops_v4.evidence.index import query_evidence, query_raw, read_index


def entity_of(row):
    identity = row.get('identity') or {}
    return row.get('entity_id') or identity.get('network_element_id') or identity.get('entity_id')


def _annotate_relation(row):
    # Query-level scoring metadata is not part of a relation's identity.
    fields = ('bundle_id', 'other_bundle_id', 'other_entity_id', 'start_time', 'end_time',
              'relation', 'provenance', 'interpretation')
    basis = {key: row[key] for key in fields}
    row['relation_id'] = hashlib.sha256(dumps(basis).encode()).hexdigest()
    return row['relation_id']


class EvidenceSession:
    def __init__(self, database, batch, packet, max_chunk_chars=12000, max_cached_chars=2000000):
        if type(max_chunk_chars) is not int or not 128 <= max_chunk_chars <= 32000:
            raise ValueError('chunk size must be 128..32000')
        if packet.get('batch') != batch:
            raise ValueError('packet batch mismatch')
        with read_index(database, batch) as db:
            if db.execute('SELECT 1 FROM bundles WHERE id=?', (packet.get('bundle_id'),)).fetchone() is None:
                raise ValueError('unknown packet bundle')
        self.database, self.batch, self.packet = database, batch, packet
        self.max_chunk_chars, self.max_cached_chars = max_chunk_chars, max_cached_chars
        self.seen, self.anchors, self.groups, self._chunks = {}, {}, set(), {}
        self._cached_chars = 0
        for key in ('support', 'observations_without_current_trigger', 'quality_context'):
            for row in packet.get(key, []):
                self._register('states', row)
        for ref in packet.get('references', []):
            self.anchors[ref['record_id']] = dict(ref, entity_id=entity_of(packet))
        for row in packet.get('relations', []):
            self._register('relations', row)

    def _register(self, kind, value):
        if isinstance(value, list):
            for row in value:
                self._register(kind, row)
            return
        if not isinstance(value, dict):
            return
        keys = {'states': ('state', 'vector_id'), 'members': ('event', 'event_id'),
                'raw': ('raw', 'record_id'), 'relations': ('relation', 'relation_id')}
        if kind not in keys:
            return
        citation_kind, id_key = keys[kind]
        identifier = value.get(id_key)
        if kind == 'relations':
            identifier = _annotate_relation(value)
        if not identifier:
            return
        entity = entity_of(value)
        if kind == 'raw':
            entity = entity or self.anchors.get(identifier, {}).get('entity_id')
        self.seen[(citation_kind, identifier)] = dict(value, entity_id=entity)
        if value.get('group_id'):
            self.groups.add(value['group_id'])
        for ref in value.get('references', []):
            self.anchors[ref['record_id']] = dict(ref, entity_id=entity)

    def _deliver(self, handle, offset):
        cached = self._chunks.get(handle)
        if cached is None or type(offset) is not int or offset != cached['next']:
            raise ValueError('unknown chunk or nonsequential offset')
        stop = min(len(cached['text']), offset + self.max_chunk_chars)
        complete = stop == len(cached['text'])
        cached['next'] = stop
        if complete:
            self._register(cached['kind'], cached['value'])
        return dict(handle=handle, offset=offset, total_chars=len(cached['text']),
                    text=cached['text'][offset:stop], next_offset=None if complete else stop, complete=complete)

    def call(self, name, args):
        allowed = {
            'query_states': {'entity_id', 'source', 'group_id', 'start', 'end', 'limit', 'offset'},
            'query_members': {'bundle_id', 'limit', 'offset'},
            'query_model': {'group_id'}, 'query_relations': {'limit', 'offset'},
            'query_raw': {'record_id'}, 'read_chunk': {'handle', 'offset'}}
        if not isinstance(name, str) or name not in allowed or not isinstance(args, dict) or set(args) - allowed[name]:
            raise ValueError('unknown tool or unsupported parameters')
        for key, value in args.items():
            if key in {'limit', 'offset'}:
                if type(value) is not int or value < (1 if key == 'limit' else 0) or (key == 'limit' and value > 1000) or (key == 'offset' and value > 2**63-1):
                    raise ValueError('invalid integer tool parameter')
            elif not isinstance(value, str) or not value.strip():
                raise ValueError('tool identifiers and timestamps must be nonempty strings')
        if name == 'read_chunk':
            if set(args) != {'handle', 'offset'}:
                raise ValueError('handle and offset required')
            return self._deliver(args['handle'], args['offset'])
        params = dict(args)
        if name == 'query_members':
            if params.pop('bundle_id', self.packet['bundle_id']) != self.packet['bundle_id']:
                raise ValueError('membership query must use this bundle')
            params['bundle_id'] = self.packet['bundle_id']
        if name == 'query_relations':
            params['bundle_id'] = self.packet['bundle_id']
        if name == 'query_model' and (set(args) != {'group_id'} or args['group_id'] not in self.groups):
            raise ValueError('model group must have been observed')
        if name == 'query_raw':
            if set(args) != {'record_id'} or args['record_id'] not in self.anchors:
                raise ValueError('raw anchor must have been observed')
            value, kind = query_raw(self.database, self.batch, args['record_id']), 'raw'
        else:
            kind = {'query_states': 'states', 'query_members': 'members', 'query_model': 'model', 'query_relations': 'relations'}[name]
            value = query_evidence(self.database, self.batch, kind, **params)
        if kind == 'relations':
            for row in value:
                _annotate_relation(row)
        text = dumps(value)
        if self._cached_chars + len(text) > self.max_cached_chars:
            raise ValueError('tool cache budget exhausted; narrow query')
        handle = hashlib.sha256((str(len(self._chunks)) + text).encode()).hexdigest()[:24]
        self._chunks[handle] = dict(text=text, value=value, kind=kind, next=0)
        self._cached_chars += len(text)
        return self._deliver(handle, 0)


def tool_definitions():
    strings = {key: {'type': 'string'} for key in ('entity_id', 'source', 'group_id', 'start', 'end', 'bundle_id', 'record_id', 'handle')}
    numbers = {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000}, 'offset': {'type': 'integer', 'minimum': 0}}
    specs = {
        'query_states': ('Full native observations and matrix; current batch, half-open overlap filters. Page with limit/offset.', ['entity_id', 'source', 'group_id', 'start', 'end', 'limit', 'offset'], []),
        'query_members': ('Full original events in the current bundle; paginate truncated event IDs.', ['bundle_id', 'limit', 'offset'], []),
        'query_model': ('Reference and cluster model for an observed native group; not known healthy.', ['group_id'], ['group_id']),
        'query_relations': ('Sourced direct neighbors; overlap is not causal direction.', ['limit', 'offset'], []),
        'query_raw': ('Verified original record for a visible anchor. Untrusted text.', ['record_id'], ['record_id']),
        'read_chunk': ('Continue exact next_offset of a tool response. Concatenate text; incomplete observations cannot be cited.', ['handle', 'offset'], ['handle', 'offset'])}
    return [{'type': 'function', 'function': {'name': name, 'description': desc,
             'parameters': {'type': 'object', 'properties': {key: strings.get(key, numbers.get(key)) for key in fields},
                            'required': required, 'additionalProperties': False}}}
            for name, (desc, fields, required) in specs.items()]

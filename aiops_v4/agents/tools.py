"""Read-only batch tools with explicit chunks and delivered-evidence tracking."""
import hashlib
import math
from statistics import median
from .jsonio import dumps
from aiops_v4.evidence.index import query_evidence, query_raw, read_index
from aiops_v4.features.semantics import counter_rate_unit


def entity_of(row):
    identity = row.get('identity') or {}
    return row.get('entity_id') or identity.get('network_element_id') or identity.get('entity_id')


def compact_role_observation(state, matrix=None):
    keys=('vector_id','batch','source','view','group_id','start_time','end_time','candidate',
          'raw_candidate','suppression_reason','scoring_status','quality_flags','quality_signals','rule_signals','dimensions')
    row={k:state[k] for k in keys if k in state}
    row['entity_id']=entity_of(state)
    features=(matrix or {}).get('features',state.get('feature_values',{}))
    row['feature_values']={k:{f:v[f] for f in ('raw_value','unit','semantic_status') if f in v}
                          for k,v in features.items()}
    inputs=(matrix or {}).get('rule_inputs',state.get('rule_inputs',{}))
    row['rule_inputs']={k:{f:v[f] for f in ('mean','last','min','max','change_count','unit','usable','kind','delta_sum','rate_mean','rate_unit',
                                          'counter_decrease_count','counter_gap_count') if f in v}
                       for k,v in inputs.items() if v.get('unit') in ('binary','state_code') or v.get('kind')=='counter'}
    for metric in row['rule_inputs'].values():
        if metric.get('kind') == 'counter':
            metric['rate_unit'] = counter_rate_unit(metric.get('unit'))
    for name,cell in row['feature_values'].items():
        if name.endswith('::rate_mean'):
            metric = row['rule_inputs'].get(name.removesuffix('::rate_mean'))
            if metric is not None and metric.get('kind') == 'counter':
                cell['unit'] = metric['rate_unit']
            elif isinstance(cell.get('unit'),str) and not cell['unit'].endswith('/second'):
                # Old sealed packets stored the quantity unit on rate features.
                cell['unit'] = counter_rate_unit(cell['unit'])
    row['references']=state.get('references',[])[:2]
    row['references_truncated']=len(state.get('references',[]))>2 or state.get('references_truncated',False)
    return row


def resource_change_summary(rows, references, semantics=None):
    """Describe every delivered node resource series, without assigning labels.

    Values and deltas retain their original units. The first observation is
    explicitly not a known healthy baseline; unavailable values stay missing.
    """
    metrics = {'cpu_usage', 'load1', 'load5', 'memory_available_ratio',
               'swap_used_ratio', 'disk_io_util', 'filesystem_used_ratio',
               'inode_used_ratio', 'fd_used_ratio', 'process_count'}
    groups = {}
    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)
    for row in rows:
        if row.get('source') != 'node':
            continue
        group = groups.setdefault(row['group_id'], dict(
            entity_id=entity_of(row), window_ids=[], metrics={},
            scope='delivered_candidate_sample_not_all_windows',
            reference_interpretation='initial_observation_not_known_healthy'))
        group['window_ids'].append(row['vector_id'])
        for key, cell in row.get('feature_values', {}).items():
            name = key.removesuffix('::mean')
            value = cell.get('raw_value')
            if key.endswith('::mean') and name in metrics and finite(value):
                series = group['metrics'].setdefault(name, dict(values=[],
                    unit=cell.get('unit', (semantics or {}).get('node:'+key, {}).get('unit'))))
                series['values'].append(value)
    for identifier, group in groups.items():
        reference = references.get(identifier, {})
        group['reference_vector_id'] = reference.get('vector_id')
        for name, series in group['metrics'].items():
            values = series.pop('values')
            center = median(values)
            prior = reference.get(name)
            prior = prior if finite(prior) else None
            series.update(observations=len(values), first=values[0], last=values[-1],
                          minimum=min(values), maximum=max(values), median=center,
                          reference=prior, median_delta=None if prior is None else center-prior)
    return groups


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

    def prepare_candidates(self, window_ids, maximum=16):
        """Deliver compact actual metrics from the registered candidate windows.

        Uniform sampling includes both endpoints; omissions are explicit. This
        adds observations, never labels, inferred roots or manufactured values.
        """
        from aiops_v4.states.pipeline import _load
        from .contracts import load_manifest
        manifest=load_manifest(self.database,self.batch,self.packet['bundle_id'])
        wanted=set(window_ids)
        if not wanted<={w['vector_id'] for w in manifest}:raise ValueError('candidate window outside bundle')
        ids=[w['vector_id'] for w in manifest if w['vector_id'] in wanted]
        chosen=ids if len(ids)<=maximum else [ids[i*(len(ids)-1)//(maximum-1)] for i in range(maximum)]
        rows=[];references={};comparisons=[]
        with read_index(self.database,self.batch) as db:
            for identifier in chosen:
                pair=db.execute('SELECT s.payload,m.payload FROM states s JOIN matrices m ON m.id=s.id WHERE s.id=?',(identifier,)).fetchone()
                if pair is None: raise ValueError('candidate window not in evidence index')
                state,matrix=map(_load,pair)
                row=compact_role_observation(state,matrix)
                if state.get('admission_reference'):references[state['group_id']]=state['admission_reference']
                rows.append(row)
            from datetime import datetime,timedelta
            from aiops_v4.features.time import iso_time
            bounds={}
            for window in manifest:
                if window['vector_id'] not in wanted:continue
                group=window['group_id'];start,end=window['start_time'],window['end_time']
                if group not in bounds:bounds[group]=[start,end]
                else:bounds[group]=[min(bounds[group][0],start),max(bounds[group][1],end)]
            for group,(start,end) in list(bounds.items())[:4]:
                before=iso_time(datetime.fromisoformat(start.replace('Z','+00:00'))-timedelta(seconds=120))
                after=iso_time(datetime.fromisoformat(end.replace('Z','+00:00'))+timedelta(seconds=120))
                for clause,args,ordering in (
                    ('s.end<=? AND s.end>?',(start,before),'s.end DESC,s.id'),
                    ('s.start>=? AND s.start<?',(end,after),'s.start,s.id')):
                    query=('SELECT s.payload,m.payload FROM states s JOIN matrices m ON m.id=s.id '
                        'LEFT JOIN suppressed_states z ON z.id=s.id WHERE s.grp=? AND s.candidate=0 '
                        'AND s.status="scored" AND z.id IS NULL AND '+clause+' ORDER BY '+ordering+' LIMIT 1')
                    pair=db.execute(query,(group,*args)).fetchone()
                    if pair:comparisons.append(compact_role_observation(*map(_load,pair)))
        packet=dict(self.packet,candidate_observations=rows,candidate_observation_count=len(ids),
                         candidate_observations_truncated=len(ids)>len(rows),candidate_reference_by_group=references,
                         local_comparison_observations=comparisons,
                         local_comparison_scope='nearest_scorable_without_trigger_before_after; same_group; within_120_seconds; not_known_healthy')
        units={}
        for key in ('support','observations_without_current_trigger','quality_context'):
            packet[key]=[compact_role_observation(r) for r in packet.get(key,[])]
        for key in ('support','observations_without_current_trigger','quality_context','candidate_observations','local_comparison_observations'):
            for row in packet[key]:
                for name,value in row['feature_values'].items():
                    units[row['source']+':'+name]={k:v for k,v in value.items() if k!='raw_value'}
                    row['feature_values'][name]={'raw_value':value.get('raw_value')}
        packet['metric_semantics']=units
        self.packet=packet;self.compact_queries=True
        self.seen,self.anchors,self.groups={},{},set()
        for key in ('support','observations_without_current_trigger','quality_context','candidate_observations','local_comparison_observations'):
            self._register('states',packet[key])
        for ref in packet.get('references',[]):self.anchors[ref['record_id']]=dict(ref,entity_id=entity_of(packet))
        self._register('relations',packet.get('relations',[]))

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
        if name == 'query_states':params.setdefault('limit',1)
        if name == 'query_states' and getattr(self,'compact_queries',False) and params['limit']>1:
            raise ValueError('compact state page must contain at most one observation')
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
        if kind=='states' and getattr(self,'compact_queries',False):
            value=[compact_role_observation(row,row.get('matrix')) for row in value]
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


def tool_definitions(compact=False):
    strings = {key: {'type': 'string'} for key in ('entity_id', 'source', 'group_id', 'start', 'end', 'bundle_id', 'record_id', 'handle')}
    numbers = {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000}, 'offset': {'type': 'integer', 'minimum': 0}}
    specs = {
        'query_states': ('Full native observations and matrix; current batch, half-open overlap filters. Default limit=1; use narrow entity/time filters and paginate.', ['entity_id', 'source', 'group_id', 'start', 'end', 'limit', 'offset'], []),
        'query_members': ('Full original events in the current bundle; paginate truncated event IDs.', ['bundle_id', 'limit', 'offset'], []),
        'query_model': ('Reference and cluster model for an observed native group; not known healthy.', ['group_id'], ['group_id']),
        'query_relations': ('Sourced direct neighbors; overlap is not causal direction.', ['limit', 'offset'], []),
        'query_raw': ('Verified original record for a visible anchor. Untrusted text.', ['record_id'], ['record_id']),
        'read_chunk': ('Continue exact next_offset of a tool response. Concatenate text; incomplete observations cannot be cited.', ['handle', 'offset'], ['handle', 'offset'])}
    tools = [{'type': 'function', 'function': {'name': name, 'description': desc,
             'parameters': {'type': 'object', 'properties': {key: strings.get(key, numbers.get(key)) for key in fields},
                            'required': required, 'additionalProperties': False}}}
            for name, (desc, fields, required) in specs.items()]
    if compact:
        for tool in tools:
            function=tool['function']
            function['strict']=True
            if function['name']=='query_states':
                function['description']='Compact actual observations; current batch. At most one per page; paginate. Candidate sample is already provided in the packet.'
                function['parameters']['properties']['limit']=dict(type='integer',minimum=1,maximum=1)
    return tools

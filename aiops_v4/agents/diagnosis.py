"""Independent localization and classification; one later consistency gate."""
from .contracts import load_manifest, validate_event, validate_localization, validate_classification
from .engine import Budget, run_role
from .jsonio import dumps, loads
from .tools import EvidenceSession


def evidence_for(output, seen, event):
    """Keep the actual delivered facts needed to check this result again."""
    cited = list(output.get('citations', []))
    for candidate in output.get('candidates', []):
        cited.extend(candidate.get('citations', []))
    keys = {(c['kind'], c['id']) for c in cited}
    raw_ids = {c['id'] for c in cited if c['kind'] == 'raw'}
    for window in event['window_ids']:
        state = seen.get(('state', window))
        if state and any(ref['record_id'] in raw_ids for ref in state.get('references', [])):
            keys.add(('state', window))
    return [dict(kind=kind, id=identifier, data=seen[(kind, identifier)]) for kind, identifier in sorted(keys)]


def _validated_role(backend, session, role, event, payload, budget):
    run = run_role(backend, session, role, event['event_id'], payload, budget)
    output, evidence = None, []
    if run['status'] == 'completed':
        try:
            if role == 'localization':
                output = validate_localization(run['output'], session.seen)
            else:
                output = validate_classification(run['output'], event, session.seen)
            evidence = evidence_for(output, session.seen, event)
        except (ValueError, TypeError, KeyError):
            run.update(status='deferred', error_code='invalid_role_contract', output=None)
    return dict(run=run, result=output, evidence=evidence)


def diagnose_event(backend, database, batch, packet, event, budget=Budget()):
    if event.get('decision') != 'confirmed':
        raise ValueError('only confirmed events enter independent diagnosis')
    manifest = load_manifest(database, batch, packet['bundle_id'], budget.max_manifest_windows)
    validate_event(event, manifest)
    selected_manifest = [w for w in manifest if w['vector_id'] in set(event['window_ids'])]
    # Both roles get identical serialized input, each in a new conversation and tool registry.
    payload = loads(dumps(dict(bundle=packet, event=event)))
    blocks = {}
    for role in ('localization', 'classification'):
        session = EvidenceSession(database, batch, loads(dumps(packet)))
        blocks[role] = _validated_role(backend, session, role, event, loads(dumps(payload)), budget)
    resolved = all(b['result'] is not None and b['result']['decision'] == 'resolved' for b in blocks.values())
    return dict(batch=batch, bundle_id=packet['bundle_id'], event=event, event_manifest=selected_manifest, accepted=False,
                status='awaiting_review' if resolved else 'deferred', **blocks)


def evidence_registry(evidence, batch):
    from .contracts import fields
    registry = {}
    keys = {'state': 'vector_id', 'raw': 'record_id', 'event': 'event_id', 'relation': 'relation_id'}
    for proof in evidence:
        fields(proof, ('kind', 'id', 'data'))
        kind, identifier, data = proof['kind'], proof['id'], proof['data']
        if (kind not in keys or not isinstance(identifier, str) or not isinstance(data, dict)
                or data.get(keys[kind]) != identifier or data.get('batch', batch) != batch):
            raise ValueError('invalid delivered evidence proof')
        registry[(kind, identifier)] = data
    return registry


def validate_independent_diagnosis(item):
    from .contracts import validate_event
    event = validate_event(item['event'], item['event_manifest'])
    common_backend = None
    for role in ('localization', 'classification'):
        block, case_id = item[role], event['event_id']
        run, output = block['run'], block['result']
        if (run['status'] != 'completed' or output is None or output.get('decision') != 'resolved'
                or run['output'] != output or run['role'] != role or run['case_id'] != case_id
                or run['model'] != run['backend']['model']):
            raise ValueError('independent role incomplete or altered')
        if common_backend is not None and common_backend != run['backend']:
            raise ValueError('independent roles must share configured backend and model')
        common_backend = run['backend']
        seen = evidence_registry(block['evidence'], item['batch'])
        if role == 'localization':
            validate_localization(output, seen)
        else:
            validate_classification(output, event, seen)
    return common_backend


def review_event(backend, database, batch, packet, item, budget=Budget()):
    from .contracts import load_manifest, validate_event, validate_review
    result = loads(dumps(item))
    result.update(accepted=False, status='deferred')
    try:
        common = validate_independent_diagnosis(result)
        if common != backend.metadata() or result['batch'] != batch or result['bundle_id'] != packet['bundle_id']:
            raise ValueError('review backend or scope mismatch')
        manifest = load_manifest(database, batch, packet['bundle_id'], budget.max_manifest_windows)
        validate_event(result['event'], manifest)
        if result['event_manifest'] != [w for w in manifest if w['vector_id'] in set(result['event']['window_ids'])]:
            raise ValueError('registered event manifest changed')
    except (ValueError, KeyError, TypeError):
        result['review'] = dict(run=None, result=None, evidence=[], error_code='incomplete_or_invalid_independent_roles')
        return result
    session = EvidenceSession(database, batch, loads(dumps(packet)))
    proofs = result['localization']['evidence'] + result['classification']['evidence']
    mapping = {'state': 'states', 'raw': 'raw', 'event': 'members', 'relation': 'relations'}
    for proof in proofs:
        session._register(mapping[proof['kind']], proof['data'])
    payload = dict(bundle=packet, event=result['event'], independent_diagnoses={
        role: {key: result[role][key] for key in ('result', 'evidence')}
        for role in ('localization', 'classification')})
    run = run_role(backend, session, 'review', result['event']['event_id'], payload, budget)
    output, evidence = None, []
    if run['status'] == 'completed':
        try:
            output = validate_review(run['output'], result['event'], session.seen)
            evidence = evidence_for(output, session.seen, result['event'])
        except (ValueError, KeyError, TypeError):
            run.update(status='deferred', error_code='invalid_role_contract', output=None)
    result['review'] = dict(run=run, result=output, evidence=evidence)
    if output is not None:
        result['accepted'] = output['decision'] == 'accept'
        result['status'] = {'accept': 'accepted', 'reject': 'rejected', 'defer': 'deferred'}[output['decision']]
    return result

"""Independent localization and classification; one later consistency gate."""
from .contracts import validate_localization, validate_classification
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
    # Both roles get identical serialized input, each in a new conversation and tool registry.
    payload = loads(dumps(dict(bundle=packet, event=event)))
    blocks = {}
    for role in ('localization', 'classification'):
        session = EvidenceSession(database, batch, loads(dumps(packet)))
        blocks[role] = _validated_role(backend, session, role, event, loads(dumps(payload)), budget)
    resolved = all(b['result'] is not None and b['result']['decision'] == 'resolved' for b in blocks.values())
    return dict(batch=batch, bundle_id=packet['bundle_id'], event=event, accepted=False,
                status='awaiting_review' if resolved else 'deferred', **blocks)

"""Validated public predictions; explanations remain in diagnosis artifacts.

Two export policies share the same evidence contract:

* a strictly accepted diagnosis (all four LLM roles completed and the reviewer
  accepted it), optionally with an evidence-backed category correction;
* an explicit opt-in abstention fallback that never discards a confirmed event
  merely because a later role deferred. The fallback only uses facts that were
  actually delivered to the confirmation role and never invents a device.
"""
from aiops_challenge_2026.schema import VALID_NETWORK_ELEMENTS, validate_prediction
from .contracts import validate_review, validate_localization, validate_classification
from .diagnosis import validate_independent_diagnosis, validate_role_record, validate_confirmation_record
from .evidence_priors import adjust_category, prior_category, prior_root


def _classification_seen(item):
    return validate_role_record(item['classification'], 'classification', item)


def _localization_seen(item):
    return validate_role_record(item['localization'], 'localization', item)


def _confirmation_seen(item):
    from .engine import recorded_delivery
    return recorded_delivery(item['confirmation']['run'], 'confirmation', item['bundle_id'])


def _effective_category(item, root, seen):
    output = item['classification']['result']
    pair = (output['category']['major_category'], output['category']['sub_category'])
    pair, adjustment = adjust_category(pair, root, seen, item['event'])
    if adjustment:
        item['category_adjustment'] = adjustment
        item['effective_category'] = dict(major_category=pair[0], sub_category=pair[1])
    return dict(major_category=pair[0], sub_category=pair[1])


def _strict_prediction(item):
    try:
        if item.get('diagnostic_mode') != 'independent' or item.get('review_mode') != 'llm':
            raise ValueError('strict export requires independent LLM review mode')
        common_backend = validate_independent_diagnosis(item)
        block = item['review']; run, output = block['run'], block['result']
        if (item['status'] != 'accepted' or run is None or run['status'] != 'completed' or output is None
                or run['output'] != output or run['role'] != 'review' or run['case_id'] != item['event']['event_id']
                or run['model'] != common_backend['model'] or run['backend'] != common_backend):
            raise ValueError('accepted diagnosis must have complete consistent review')
        payload, seen = validate_role_record(block, 'review', item)
        if payload['independent_diagnoses'] != {r: {k: item[r][k] for k in ('result', 'evidence')}
                                               for r in ('localization', 'classification')}:
            raise ValueError('reviewed answers changed')
        validate_review(output, item['event'], seen)
        if output['decision'] != 'accept':
            raise ValueError('review did not accept')
        event = item['event']
        localization = item['localization']['result']
        _, classification_seen = _classification_seen(item)
        root = localization['candidates'][0]['network_element_id']
        category = _effective_category(item, root, classification_seen)
        return format_prediction(event, localization, dict(category=category))
    except (KeyError, TypeError) as error:
        raise ValueError('malformed accepted diagnosis') from error


def _ranked_fallback_roots(root, seen, event):
    roots = [root]
    candidates = {}
    for (kind, identifier), row in (seen or {}).items():
        if kind != 'state' or identifier not in set(event.get('window_ids') or ()):
            continue
        entity = row.get('entity_id') or (row.get('identity') or {}).get('network_element_id')
        if not isinstance(entity, str) or entity not in VALID_NETWORK_ELEMENTS or entity.endswith('-traffic-vm'):
            continue
        if entity == root:
            continue
        candidates[entity] = candidates.get(entity, 0) + len(row.get('drivers') or ()) + 1
    for entity, _ in sorted(candidates.items(), key=lambda item: (-item[1], item[0])):
        if len(roots) >= 5:
            break
        roots.append(entity)
    return roots


def _fallback_prediction(item):
    event = validate_confirmation_record(item)
    review = item.get('review') or {}
    output = review.get('result')
    if output is not None and output.get('decision') == 'reject':
        return None
    payload, seen = _confirmation_seen(item)
    packet = payload.get('bundle') or {}
    localization = (item.get('localization') or {}).get('result')
    classification = (item.get('classification') or {}).get('result')
    root = None
    roots = []
    if localization is not None and localization.get('decision') == 'resolved':
        try:
            _, location_seen = _localization_seen(item)
            validate_localization(localization, location_seen)
            roots = [candidate['network_element_id'] for candidate in localization['candidates']]
            root = roots[0]
        except (ValueError, KeyError, TypeError):
            root = None; roots = []
    if root is None:
        root = prior_root(event, packet, seen)
        roots = [root] if root else []
    if root is None:
        return None
    category = None
    if classification is not None and classification.get('decision') == 'resolved':
        try:
            _, classification_seen = _classification_seen(item)
            validate_classification(classification, event, classification_seen)
            pair = (classification['category']['major_category'], classification['category']['sub_category'])
            pair, adjustment = adjust_category(pair, root, classification_seen, event)
            category = dict(major_category=pair[0], sub_category=pair[1])
            if adjustment:
                item['category_adjustment'] = adjustment
        except (ValueError, KeyError, TypeError):
            category = None
    if category is None:
        pair = prior_category(event, root, seen)
        if pair is None:
            return None
        category = dict(major_category=pair[0], sub_category=pair[1])
    roots = _ranked_fallback_roots(root, seen, event) if len(roots) < 2 else roots
    item['export_basis'] = 'confirmed_event_evidence_fallback'
    item['fallback'] = dict(root=root, roots=roots, category=category,
                            localization_resolved=bool(localization and localization.get('decision') == 'resolved'),
                            classification_resolved=bool(classification and classification.get('decision') == 'resolved'))
    record = format_prediction(event, dict(candidates=[dict(network_element_id=node) for node in roots]),
                               dict(category=category))
    return record


def prediction_for(item, *, export_abstentions=False):
    if item.get('accepted'):
        return _strict_prediction(item)
    if not export_abstentions:
        return None
    return _fallback_prediction(item)


def format_prediction(event, localization, classification):
    record = dict(prediction_id=event['event_id'], start_time=event['start_time'], end_time=event['end_time'],
                  root_cause_top5=[dict(rank=i + 1, network_element_id=c['network_element_id'])
                                   for i, c in enumerate(localization['candidates'][:5])],
                  fault_category=classification['category'])
    validate_prediction(record)
    return record


def export_predictions(diagnoses, *, export_abstentions=False):
    identifiers = set()
    for item in diagnoses:
        record = prediction_for(item, export_abstentions=export_abstentions)
        if record is None:
            continue
        if record['prediction_id'] in identifiers:
            raise ValueError('duplicate prediction_id')
        identifiers.add(record['prediction_id'])
        yield record

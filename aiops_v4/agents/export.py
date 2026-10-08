"""Validated public predictions; explanations remain in diagnosis artifacts."""
from aiops_challenge_2026.schema import validate_prediction
from .contracts import validate_review
from .diagnosis import evidence_registry, validate_independent_diagnosis


def prediction_for(item):
    if not item.get('accepted'):
        return None
    try:
        common_backend = validate_independent_diagnosis(item)
        block = item['review']; run, output = block['run'], block['result']
        if (item['status'] != 'accepted' or run is None or run['status'] != 'completed' or output is None
                or run['output'] != output or run['role'] != 'review' or run['case_id'] != item['event']['event_id']
                or run['model'] != common_backend['model'] or run['backend'] != common_backend):
            raise ValueError('accepted diagnosis must have complete consistent review')
        validate_review(output, item['event'], evidence_registry(block['evidence'], item['batch']))
        if output['decision'] != 'accept':
            raise ValueError('review did not accept')
        event = item['event']
        record = dict(prediction_id=event['event_id'], start_time=event['start_time'], end_time=event['end_time'],
                      root_cause_top5=[dict(rank=i + 1, network_element_id=c['network_element_id'])
                                      for i, c in enumerate(item['localization']['result']['candidates'][:5])],
                      fault_category=item['classification']['result']['category'])
        validate_prediction(record)
        return record
    except (KeyError, TypeError) as error:
        raise ValueError('malformed accepted diagnosis') from error


def export_predictions(diagnoses):
    identifiers = set()
    for item in diagnoses:
        record = prediction_for(item)
        if record is None:
            continue
        if record['prediction_id'] in identifiers:
            raise ValueError('duplicate prediction_id')
        identifiers.add(record['prediction_id'])
        yield record

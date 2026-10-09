"""Validated public predictions; explanations remain in diagnosis artifacts."""
from aiops_challenge_2026.schema import validate_prediction
from .contracts import validate_review
from .diagnosis import validate_independent_diagnosis, validate_role_record


def prediction_for(item):
    if not item.get('accepted'):
        return None
    try:
        if item.get('diagnostic_mode')!='independent' or item.get('review_mode')!='llm':
            raise ValueError('strict export requires independent LLM review mode')
        common_backend = validate_independent_diagnosis(item)
        block = item['review']; run, output = block['run'], block['result']
        if (item['status'] != 'accepted' or run is None or run['status'] != 'completed' or output is None
                or run['output'] != output or run['role'] != 'review' or run['case_id'] != item['event']['event_id']
                or run['model'] != common_backend['model'] or run['backend'] != common_backend):
            raise ValueError('accepted diagnosis must have complete consistent review')
        payload,seen=validate_role_record(block,'review',item)
        if payload['independent_diagnoses']!={r:{k:item[r][k] for k in ('result','evidence')}
            for r in ('localization','classification')}:raise ValueError('reviewed answers changed')
        validate_review(output, item['event'], seen)
        if output['decision'] != 'accept':
            raise ValueError('review did not accept')
        event = item['event']
        return format_prediction(event,item['localization']['result'],item['classification']['result'])
    except (KeyError, TypeError) as error:
        raise ValueError('malformed accepted diagnosis') from error


def format_prediction(event,localization,classification):
    record = dict(prediction_id=event['event_id'], start_time=event['start_time'], end_time=event['end_time'],
                  root_cause_top5=[dict(rank=i + 1, network_element_id=c['network_element_id'])
                                  for i, c in enumerate(localization['candidates'][:5])],
                  fault_category=classification['category'])
    validate_prediction(record)
    return record


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

"""Explicit role experiments; never manufacture independent or review traces."""
from aiops_challenge_2026.schema import validate_prediction
from aiops_v4.agents.contracts import (fields, text, missing, citations, load_manifest, validate_event,
    validate_localization, validate_classification, validate_review)
from aiops_v4.agents.diagnosis import (diagnose_event, review_event, evidence_for, evidence_registry,
    validate_independent_diagnosis)
from aiops_v4.agents.engine import Budget, run_role
from aiops_v4.agents.jsonio import dumps, loads
from aiops_v4.agents.tools import EvidenceSession
from .knowledge import knowledge_cards


def role_session(database,batch,packet,context):
    session=EvidenceSession(database,batch,loads(dumps(packet)))
    if context and context.get('state_explanation'):
        mapping={'state':'states','raw':'raw','event':'members','relation':'relations'}
        proofs=context['state_explanation']['evidence']
        evidence_registry(proofs,batch)
        for proof in proofs: session._register(mapping[proof['kind']],proof['data'])
    return session


def explain_state(backend,database,batch,packet,manifest,budget,context):
    session=EvidenceSession(database,batch,packet)
    run=run_role(backend,session,'state_explanation',packet['bundle_id'],dict(bundle=packet,manifest=manifest,context=context),budget)
    block=None
    if run['status']=='completed':
        try:
            output=run['output']; fields(output,('summary','citations','limits'));text(output['summary'])
            missing(output['limits'],deferred=True)
            found=citations(output['citations'],session.seen)
            if not any(c['kind'] in ('state','raw') for c,_ in found): raise ValueError('observation required')
            block=dict(result=output,evidence=evidence_for(output,session.seen,dict(window_ids=[w['vector_id'] for w in manifest])))
        except (ValueError,KeyError,TypeError): run.update(status='deferred',output=None,error_code='invalid_role_contract')
    return run,block


def _joint(backend,database,batch,packet,event,budget,context):
    manifest=load_manifest(database,batch,packet['bundle_id'],budget.max_manifest_windows)
    validate_event(event,manifest)
    selected=[w for w in manifest if w['vector_id'] in set(event['window_ids'])]
    session=role_session(database,batch,packet,context)
    run=run_role(backend,session,'joint_diagnosis',event['event_id'],dict(bundle=packet,event=event,context=context),budget)
    outputs=dict(localization=None,classification=None);proofs=dict(localization=[],classification=[])
    if run['status']=='completed':
        try:
            fields(run['output'],('localization','classification'))
            left=validate_localization(run['output']['localization'],session.seen)
            right=validate_classification(run['output']['classification'],event,session.seen)
            outputs=dict(localization=left,classification=right)
            proofs={r:evidence_for(o,session.seen,event) for r,o in outputs.items()}
        except (ValueError,KeyError,TypeError):run.update(status='deferred',output=None,error_code='invalid_role_contract')
    return dict(batch=batch,bundle_id=packet['bundle_id'],event=event,event_manifest=selected,
                diagnostic_mode='joint',accepted=False,status='deferred',
                joint=dict(run=run,result=run['output']),
                **{r:dict(run=None,result=outputs[r],evidence=proofs[r],source='joint_diagnosis') for r in outputs})


def _validate(item):
    if item['diagnostic_mode']=='independent': return validate_independent_diagnosis(item)
    if item['diagnostic_mode']!='joint':raise ValueError('invalid experiment mode')
    event=validate_event(item['event'],item['event_manifest'])
    block=item['joint'];run,output=block['run'],block['result']
    if (run['status']!='completed' or output is None or run['output']!=output or run['role']!='joint_diagnosis'
        or run['case_id']!=event['event_id'] or run['model']!=run['backend']['model']):raise ValueError('incomplete joint role')
    fields(output,('localization','classification'))
    for role in ('localization','classification'):
        part=item[role]
        if part['run'] is not None or part['source']!='joint_diagnosis' or part['result']!=output[role] or output[role]['decision']!='resolved':
            raise ValueError('joint answer unresolved or altered')
        seen=evidence_registry(part['evidence'],item['batch'])
        if role=='localization':validate_localization(output[role],seen)
        else:validate_classification(output[role],event,seen)
    return run['backend']


def diagnose_case(backend,database,batch,packet,event,budget,context,mode,review_mode):
    if mode=='independent' and review_mode=='llm':
        return review_event(backend,database,batch,packet,
            diagnose_event(backend,database,batch,packet,event,budget,context=context),budget,context=context)
    item=(_joint(backend,database,batch,packet,event,budget,context) if mode=='joint'
          else diagnose_event(backend,database,batch,packet,event,budget,context=context))
    item.update(diagnostic_mode=mode,review_mode=review_mode,accepted=False,status='deferred')
    item['review']=dict(run=None,result=None,evidence=[],mode=review_mode)
    try:
        common=_validate(item)
        if common!=backend.metadata():raise ValueError('backend mismatch')
        manifest=load_manifest(database,batch,packet['bundle_id'],budget.max_manifest_windows)
        validate_event(event,manifest)
        if item['event_manifest']!=[w for w in manifest if w['vector_id'] in set(event['window_ids'])]:raise ValueError('manifest altered')
    except (ValueError,KeyError,TypeError):
        item['review']['error_code']='incomplete_or_invalid_diagnostic_roles'
        return item
    if review_mode=='program_only':
        item.update(accepted=True,status='accepted')
        item['review']['reason']='Structural/event/citation validation only; no semantic LLM review.'
        return item
    session=role_session(database,batch,packet,context)
    mapping={'state':'states','raw':'raw','event':'members','relation':'relations'}
    for role in ('localization','classification'):
        for proof in item[role]['evidence']:session._register(mapping[proof['kind']],proof['data'])
    payload=dict(bundle=packet,event=event,diagnostic_mode=mode,context=context,
        diagnoses={r:{k:item[r][k] for k in ('result','evidence')} for r in ('localization','classification')})
    run=run_role(backend,session,'review',event['event_id'],payload,budget);output=None;proofs=[]
    if run['status']=='completed':
        try:
            output=validate_review(run['output'],event,session.seen);proofs=evidence_for(output,session.seen,event)
        except (ValueError,KeyError,TypeError):run.update(status='deferred',output=None,error_code='invalid_role_contract')
    item['review']=dict(run=run,result=output,evidence=proofs,mode=review_mode)
    if output is not None:item.update(accepted=output['decision']=='accept',status={'accept':'accepted','reject':'rejected','defer':'deferred'}[output['decision']])
    return item


def experiment_prediction_for(item):
    if not item.get('accepted'):return None
    common=_validate(item)
    if item['status']!='accepted':raise ValueError('inconsistent accepted experiment')
    review=item['review']
    if item['review_mode']=='llm':
        run,output=review['run'],review['result']
        if (run is None or run['status']!='completed' or run['role']!='review' or run['case_id']!=item['event']['event_id']
            or run['backend']!=common or run['model']!=common['model'] or run['output']!=output):raise ValueError('incomplete experiment review')
        validate_review(output,item['event'],evidence_registry(review['evidence'],item['batch']))
        if output['decision']!='accept':raise ValueError('review did not accept')
    elif item['review_mode']!='program_only' or review['mode']!='program_only' or review['run'] is not None:
        raise ValueError('explicit program-only review required')
    event=item['event']
    record=dict(prediction_id=event['event_id'],start_time=event['start_time'],end_time=event['end_time'],
        root_cause_top5=[dict(rank=i+1,network_element_id=c['network_element_id']) for i,c in enumerate(item['localization']['result']['candidates'][:5])],
        fault_category=item['classification']['result']['category'])
    validate_prediction(record)
    return record


def run_diagnosis(database,batch,output_dir,backend,*,mode='independent',review_mode='llm',knowledge=True,
                  state_explanation=False,bundle_id=None,limit_bundles=None,budget=Budget()):
    from aiops_v4.agents.pipeline import diagnose
    if type(knowledge) is not bool:raise ValueError('knowledge boolean required')
    return diagnose(database,batch,output_dir,backend,bundle_id=bundle_id,limit_bundles=limit_bundles,budget=budget,
        context=dict(knowledge=knowledge_cards()) if knowledge else {},diagnostic_mode=mode,
        review_mode=review_mode,state_explanation=state_explanation)

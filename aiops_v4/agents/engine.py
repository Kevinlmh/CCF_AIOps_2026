"""Fresh bounded role conversations; orchestration stays in ordinary code."""
from dataclasses import asdict, dataclass
import hashlib
from time import monotonic
from .backend import BackendError, _message, _usage
from .jsonio import dumps, loads
from .prompts import PROMPT_VERSION, system_prompt
from .tools import tool_definitions
from .structured import FINAL_TOOL, STRUCTURED_INSTRUCTION, role_tools, validate_result, contract_feedback, compact_context, canonical_result

JSON_REPAIR_MESSAGE = '上一条最终输出不是合法 JSON 对象。请重新输出一个符合当前角色格式的 JSON 对象，不要 Markdown，不要多余结束符。保持相同证据约束，不得编造或补齐结论。'


def _object_json(content):
    output=loads(content)
    if not isinstance(output,dict):raise ValueError('object required')
    return output


@dataclass(frozen=True)
class Budget:
    max_turns: int = 6
    max_tool_calls: int = 12
    max_prompt_chars: int = 120000
    max_response_chars: int = 32000
    max_manifest_windows: int = 2000

    def __post_init__(self):
        for key, value in asdict(self).items():
            if type(value) is not int or value < (0 if key == 'max_tool_calls' else 1):
                raise ValueError('invalid role budget: ' + key)


def run_role(backend, session, role, case_id, payload, budget=Budget()):
    started = monotonic()
    structured = bool(getattr(backend, 'structured_roles', False))
    if structured:
        ids=([w['vector_id'] for w in payload['manifest']] if 'manifest' in payload else
             (payload.get('event') or {}).get('window_ids',[]))
        session.prepare_candidates(ids)
        supplied=payload.get('independent_diagnoses') or payload.get('diagnoses') or {}
        blocks=[supplied[key] for key in sorted(supplied)]
        explanation=(payload.get('context') or {}).get('state_explanation')
        if explanation: blocks.append(explanation)
        mapping={'state':'states','raw':'raw','event':'members','relation':'relations'}
        for block in blocks:
            for proof in block['evidence']:session._register(mapping[proof['kind']],proof['data'])
        if 'context' in payload:payload=dict(payload,context=compact_context(payload['context']))
        if role=='confirmation':payload=dict(payload,candidate_window_aliases={
            'w'+str(i):w['vector_id'] for i,w in enumerate(payload['manifest'])})
    if 'bundle' in payload:payload=dict(payload,bundle=session.packet)
    prompt=system_prompt(role)+ ('\n'+STRUCTURED_INSTRUCTION if structured else '')
    messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': dumps(payload)}]
    tools, trace, usage, call_count, repairs, query_turns = tool_definitions(), [], {}, 0, 0, 0
    if structured: tools=role_tools(role,payload,session.seen)
    result = dict(run_schema_version=3 if structured else 2, initial_messages=loads(dumps(messages)), initial_tools=loads(dumps(tools)), role=role, case_id=case_id, status='deferred', output=None, error_code=None,
                  model=backend.model, backend=backend.metadata(), prompt_version=PROMPT_VERSION,
                  budget=asdict(budget), trace=trace, usage=usage, backend_calls=0, usage_reported_calls=0)
    try:
        for turn in range(budget.max_turns):
            if structured: tools=role_tools(role,payload,session.seen,query_turns)
            request = dict(messages=messages, tools=tools, model=backend.model)
            serialized = dumps(request)
            if len(serialized) > budget.max_prompt_chars:
                raise BackendError('prompt_char_budget')
            result['backend_calls'] += 1
            completion = backend.complete(messages, tools, role=role, case_id=case_id, turn=turn)
            result['usage_reported_calls'] += int('total_tokens' in completion.usage)
            response_text = dumps(completion.message)
            for key, count in completion.usage.items():
                usage[key] = usage.get(key, 0) + count
            if len(response_text) > budget.max_response_chars:
                raise BackendError('response_char_budget')
            entry = dict(turn=turn, request_sha256=hashlib.sha256(serialized.encode()).hexdigest(),
                         response=completion.message, actual_model=completion.model, usage=completion.usage, tools=[])
            trace.append(entry)
            calls = completion.message.get('tool_calls') or []
            if structured and not calls: raise BackendError('structured_result_required')
            if not calls:
                try:
                    output = _object_json(completion.message['content'])
                except (ValueError, RecursionError):
                    if repairs == 0 and turn+1 < budget.max_turns:
                        repairs+=1;entry['repair']='invalid_role_json'
                        messages.append(completion.message)
                        messages.append(dict(role='user',content=JSON_REPAIR_MESSAGE))
                        continue
                    raise BackendError('invalid_role_json') from None
                result.update(status='completed', output=output)
                break
            if call_count + len(calls) > budget.max_tool_calls:
                raise BackendError('tool_call_budget')
            terminal=[c for c in calls if c['function']['name']==FINAL_TOOL]
            if structured and query_turns>=2 and not terminal:raise BackendError('query_turn_budget')
            if structured and terminal:
                if len(calls)!=1: raise BackendError('mixed_terminal_tools')
                call_count+=1; call=terminal[0]; arguments=loads(call['function']['arguments'])
                record=dict(name=FINAL_TOOL,arguments=arguments,terminal=True);entry['tools'].append(record)
                try:
                    output=canonical_result(role,arguments,payload)
                    validate_result(role,output,payload,session.seen)
                except (ValueError,KeyError,TypeError) as error:
                    if repairs == 0 and turn+1 < budget.max_turns:
                        repairs+=1;entry['repair']='invalid_role_contract'
                        record['feedback']=contract_feedback(error)
                        messages.append(completion.message)
                        messages.append(dict(role='tool',tool_call_id=call['id'],content=dumps(record['feedback'])))
                        continue
                    raise BackendError('invalid_role_contract') from None
                result.update(status='completed',output=output)
                break
            query_turns+=1
            messages.append(completion.message)
            for call in calls:
                call_count += 1
                function = call['function']
                try:
                    args = loads(function['arguments'])
                    reply = session.call(function['name'], args)
                except (ValueError, TypeError, OSError, RecursionError, OverflowError):
                    entry['tools'].append(dict(name=function['name'], arguments=function['arguments'], error_code='invalid_tool_query'))
                    raise BackendError('invalid_tool_query') from None
                entry['tools'].append(dict(name=function['name'], arguments=args, result=reply))
                messages.append(dict(role='tool', tool_call_id=call['id'], content=dumps(reply)))
        else:
            raise BackendError('turn_budget')
    except BackendError as error:
        result['error_code'] = str(error)
    result['tool_calls'] = call_count
    result['elapsed_seconds'] = round(monotonic() - started, 6)
    return result


def recorded_delivery(run, role, case_id):
    """Replay the recorded protocol, not the backend, to recover delivered facts."""
    from .tools import EvidenceSession, entity_of, resource_change_summary
    try:
        if not isinstance(run,dict):raise ValueError('recorded role is missing')
        structured=run.get('run_schema_version')==3
        if (run.get('run_schema_version') not in (2,3) or structured!=run['backend'].get('structured_roles',False) or run['status']!='completed' or run['role']!=role
            or run['case_id']!=case_id or run['model']!=run['backend']['model']):
            raise ValueError('complete recorded role required')
        trace=run['trace'];budget=Budget(**run['budget'])
        if (not isinstance(trace,list) or not trace or type(run['backend_calls']) is not int
            or len(trace)!=run['backend_calls'] or len(trace)>budget.max_turns):
            raise ValueError('role trace/call count mismatch')
        messages=loads(dumps(run['initial_messages']));tools=run['initial_tools']
        if (len(messages)!=2 or messages[0]!={'role':'system','content':system_prompt(role)+('\n'+STRUCTURED_INSTRUCTION if structured else '')}
            or messages[1]['role']!='user'):
            raise ValueError('invalid recorded initial request')
        payload=loads(messages[1]['content']);packet=payload.get('bundle',{})
        if structured and 'resource_change_summary' in packet and packet['resource_change_summary']!=resource_change_summary(
                packet.get('candidate_observations',[]),packet.get('candidate_reference_by_group',{}),
                packet.get('metric_semantics',{})):
            raise ValueError('recorded resource comparison differs from delivered observations')
        registry=EvidenceSession.__new__(EvidenceSession)
        registry.seen,registry.anchors,registry.groups={}, {}, set()
        for name in ('support','observations_without_current_trigger','quality_context','candidate_observations','local_comparison_observations'):
            registry._register('states',packet.get(name,[]))
        for ref in packet.get('references',[]):registry.anchors[ref['record_id']]=dict(ref,entity_id=entity_of(packet))
        registry._register('relations',packet.get('relations',[]))
        mapping={'state':'states','raw':'raw','event':'members','relation':'relations'}
        supplied=payload.get('independent_diagnoses') or payload.get('diagnoses') or {}
        blocks=[supplied[key] for key in sorted(supplied)]
        explanation=(payload.get('context') or {}).get('state_explanation')
        if explanation:blocks.append(explanation)
        for block in blocks:
            for proof in block['evidence']:registry._register(mapping[proof['kind']],proof['data'])
        expected_tools=role_tools(role,payload,registry.seen) if structured else tool_definitions()
        if tools!=expected_tools:raise ValueError('invalid recorded initial tools')
        chunks={};call_count=0;usage={};reported=0;repairs=0;query_turns=0
        kinds={'query_states':'states','query_members':'members','query_relations':'relations','query_raw':'raw','query_model':'model'}
        for turn,entry in enumerate(trace):
            if structured:tools=role_tools(role,payload,registry.seen,query_turns)
            request=dict(messages=messages,tools=tools,model=run['model'])
            serialized=dumps(request)
            if (entry['turn']!=turn or entry['request_sha256']!=hashlib.sha256(serialized.encode()).hexdigest()
                or len(serialized)>budget.max_prompt_chars or not isinstance(entry['actual_model'],str) or not entry['actual_model']):
                raise ValueError('recorded request changed')
            response=_message(entry['response']);token_usage=_usage(entry['usage'])
            if token_usage!=entry['usage'] or len(dumps(response))>budget.max_response_chars:
                raise ValueError('invalid recorded response/usage')
            reported+=int('total_tokens' in token_usage)
            for key,count in token_usage.items():usage[key]=usage.get(key,0)+count
            calls=response.get('tool_calls',[])
            if len(calls)!=len(entry['tools']):raise ValueError('recorded tool count changed')
            if structured and not calls:raise ValueError('structured result required')
            terminal=[c for c in calls if c['function']['name']==FINAL_TOOL]
            if structured and query_turns>=2 and not terminal:raise ValueError('query turn budget changed')
            if structured and terminal:
                if len(calls)!=1:raise ValueError('mixed terminal tools')
                call=terminal[0];record=entry['tools'][0];arguments=loads(call['function']['arguments'])
                if (record.get('terminal') is not True or record['name']!=FINAL_TOOL or record['arguments']!=arguments):
                    raise ValueError('recorded terminal changed')
                call_count+=1
                if 'repair' in entry:
                    if entry['repair']!='invalid_role_contract' or repairs or turn==len(trace)-1:
                        raise ValueError('invalid recorded contract repair')
                    try:
                        output=canonical_result(role,arguments,payload)
                        validate_result(role,output,payload,registry.seen)
                    except (ValueError,KeyError,TypeError) as error:feedback=contract_feedback(error)
                    else:raise ValueError('contract repair must follow invalid result')
                    if record.get('feedback')!=feedback:raise ValueError('recorded feedback changed')
                    repairs+=1;messages.append(response)
                    messages.append(dict(role='tool',tool_call_id=call['id'],content=dumps(feedback)))
                    continue
                output=canonical_result(role,arguments,payload)
                validate_result(role,output,payload,registry.seen)
                if turn!=len(trace)-1 or output!=run['output']:raise ValueError('final role response changed')
                continue
            if not calls:
                if 'repair' in entry:
                    if entry['repair']!='invalid_role_json' or repairs or turn==len(trace)-1:
                        raise ValueError('invalid recorded JSON repair')
                    try:_object_json(response['content'])
                    except (ValueError,RecursionError):pass
                    else:raise ValueError('repair must follow malformed JSON')
                    repairs+=1
                    messages.append(response)
                    messages.append(dict(role='user',content=JSON_REPAIR_MESSAGE))
                    continue
                if turn!=len(trace)-1 or loads(response['content'])!=run['output']:
                    raise ValueError('final role response changed')
                continue
            if turn==len(trace)-1:raise ValueError('completed role has no final response')
            query_turns+=1
            messages.append(response)
            for call,record in zip(calls,entry['tools']):
                name=call['function']['name'];args=loads(call['function']['arguments'])
                if record['name']!=name or record['arguments']!=args or 'error_code' in record:
                    raise ValueError('recorded tool query changed')
                if structured and name=='query_states' and (
                        type(args.get('limit',1)) is not int or args.get('limit',1)!=1):
                    raise ValueError('recorded query exceeds compact state page')
                reply=record['result'];handle=reply['handle'];offset=reply['offset'];text=reply['text']
                if name=='read_chunk':
                    cached=chunks[handle]
                    if args!={'handle':handle,'offset':cached['next']}:raise ValueError('invalid recorded continuation')
                else:
                    if name not in kinds or handle in chunks or offset!=0:raise ValueError('invalid recorded tool delivery')
                    cached=chunks[handle]=dict(kind=kinds[name],text='',next=0,total=reply['total_chars'])
                if (offset!=cached['next'] or not isinstance(text,str) or reply['total_chars']!=cached['total']
                    or offset+len(text)>cached['total']):raise ValueError('invalid recorded chunk')
                cached['text']+=text;cached['next']=offset+len(text)
                complete=cached['next']==cached['total']
                if type(reply['complete']) is not bool or reply['complete']!=complete or reply['next_offset']!=(None if complete else cached['next']):
                    raise ValueError('invalid recorded completion')
                if complete:registry._register(cached['kind'],loads(cached['text']))
                messages.append(dict(role='tool',tool_call_id=call['id'],content=dumps(reply)))
                call_count+=1
        if (call_count!=run['tool_calls'] or call_count>budget.max_tool_calls or usage!=run['usage']
            or reported!=run['usage_reported_calls']):raise ValueError('recorded role totals changed')
        return payload,registry.seen
    except (KeyError,TypeError,IndexError,BackendError,RecursionError) as error:
        raise ValueError('malformed recorded role') from error

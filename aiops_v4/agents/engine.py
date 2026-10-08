"""Fresh bounded role conversations; orchestration stays in ordinary code."""
from dataclasses import asdict, dataclass
import hashlib
from time import monotonic
from .backend import BackendError
from .jsonio import dumps, loads
from .prompts import PROMPT_VERSION, system_prompt
from .tools import tool_definitions


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
    messages = [{'role': 'system', 'content': system_prompt(role)}, {'role': 'user', 'content': dumps(payload)}]
    tools, trace, usage, call_count = tool_definitions(), [], {}, 0
    result = dict(role=role, case_id=case_id, status='deferred', output=None, error_code=None,
                  model=backend.model, backend=backend.metadata(), prompt_version=PROMPT_VERSION,
                  budget=asdict(budget), trace=trace, usage=usage)
    try:
        for turn in range(budget.max_turns):
            request = dict(messages=messages, tools=tools, model=backend.model)
            serialized = dumps(request)
            if len(serialized) > budget.max_prompt_chars:
                raise BackendError('prompt_char_budget')
            completion = backend.complete(messages, tools, role=role, case_id=case_id, turn=turn)
            response_text = dumps(completion.message)
            for key, count in completion.usage.items():
                usage[key] = usage.get(key, 0) + count
            if len(response_text) > budget.max_response_chars:
                raise BackendError('response_char_budget')
            entry = dict(turn=turn, request_sha256=hashlib.sha256(serialized.encode()).hexdigest(),
                         response=completion.message, actual_model=completion.model, usage=completion.usage, tools=[])
            trace.append(entry)
            calls = completion.message.get('tool_calls') or []
            if not calls:
                try:
                    output = loads(completion.message['content'])
                    if not isinstance(output, dict):
                        raise ValueError('object required')
                except (ValueError, RecursionError):
                    raise BackendError('invalid_role_json') from None
                result.update(status='completed', output=output)
                break
            if call_count + len(calls) > budget.max_tool_calls:
                raise BackendError('tool_call_budget')
            messages.append(completion.message)
            for call in calls:
                call_count += 1
                function = call['function']
                try:
                    args = loads(function['arguments'])
                    reply = session.call(function['name'], args)
                except (ValueError, TypeError, OSError, RecursionError):
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

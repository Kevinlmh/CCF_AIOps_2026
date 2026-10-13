"""Bounded Chat Completions adapter and explicitly simulated replay backend."""
from dataclasses import dataclass
from http.client import HTTPException
import socket
import hashlib
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .jsonio import dumps, loads


class BackendError(RuntimeError):
    """Only sanitized error codes cross the backend boundary."""


@dataclass(frozen=True)
class Completion:
    message: dict
    usage: dict
    model: str
    wire: dict | None = None


def _message(message):
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        raise BackendError('invalid_message')
    if message.get('refusal'):
        raise BackendError('model_refusal')
    content, calls = message.get('content'), message.get('tool_calls')
    if calls is None:calls=[]
    if content is not None and not isinstance(content, str):
        raise BackendError('invalid_content')
    if not isinstance(calls, list) or len(calls) > 16:
        raise BackendError('invalid_tool_calls')
    checked, ids = [], set()
    for call in calls:
        if not isinstance(call, dict) or call.get('type') != 'function':
            raise BackendError('invalid_tool_call')
        call_id, function = call.get('id'), call.get('function')
        if not isinstance(call_id, str) or not call_id or call_id in ids or not isinstance(function, dict):
            raise BackendError('invalid_tool_call')
        name, args = function.get('name'), function.get('arguments')
        if not isinstance(name, str) or not isinstance(args, str):
            raise BackendError('invalid_tool_call')
        try:
            parsed = loads(args)
        except (ValueError, RecursionError):
            raise BackendError('invalid_tool_arguments') from None
        if not isinstance(parsed, dict):
            raise BackendError('invalid_tool_arguments')
        ids.add(call_id)
        checked.append({'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': args}})
    if not calls and not content:
        raise BackendError('empty_response')
    result = {'role': 'assistant', 'content': content}
    if checked:
        result['tool_calls'] = checked
    return result


def _usage(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BackendError('invalid_usage')
    result = {}
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        if key in value:
            if type(value[key]) is not int or value[key] < 0:
                raise BackendError('invalid_usage')
            result[key] = value[key]
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPBackend:
    def __init__(self, model, base_url, api_key, timeout=60, max_response_bytes=1048576, max_completion_tokens=4096, enable_thinking=None, temperature=None, structured_roles=False, compact_evidence=False):
        parts = urlsplit(base_url)
        if (parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or (parts.scheme == 'http' and parts.hostname not in {'localhost', '127.0.0.1', '::1'})):
            raise ValueError('base_url requires HTTPS or loopback HTTP, without credentials/query/fragment')
        if not isinstance(model, str) or not model.strip() or not isinstance(api_key, str) or not api_key:
            raise ValueError('model and API key required')
        if type(timeout) not in {int, float} or not 0 < timeout <= 300:
            raise ValueError('timeout must be 0..300 seconds')
        if type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 16777216:
            raise ValueError('response byte limit must be 1024..16777216')
        if type(max_completion_tokens) is not int or not 1 <= max_completion_tokens <= 32768:
            raise ValueError('completion token budget must be 1..32768')
        if enable_thinking is not None and type(enable_thinking) is not bool:
            raise ValueError('enable_thinking must be boolean or unset')
        self.model, self.base_url = model, base_url.rstrip('/')
        self._api_key = api_key
        self.timeout, self.max_response_bytes = timeout, max_response_bytes
        self.max_completion_tokens = max_completion_tokens
        self.enable_thinking = enable_thinking
        if temperature is not None and (type(temperature) not in {int,float} or not 0<=temperature<=2):
            raise ValueError('temperature must be 0..2 or None')
        self.temperature=temperature
        if type(structured_roles) is not bool: raise ValueError('structured_roles must be boolean')
        self.structured_roles=structured_roles
        if type(compact_evidence) is not bool or (compact_evidence and not structured_roles):
            raise ValueError("compact evidence requires structured roles")
        self.compact_evidence=compact_evidence
        self._opener = build_opener(_NoRedirect())

    def metadata(self):
        result=dict(kind='http', simulated=False, model=self.model, base_url=self.base_url,
                    timeout=self.timeout, max_response_bytes=self.max_response_bytes,
                    max_completion_tokens=self.max_completion_tokens, enable_thinking=self.enable_thinking, temperature=self.temperature, structured_roles=self.structured_roles,
                    tool_schema_order='canonical', retries=0)
        if self.compact_evidence:
            from .wire import VERSION
            result['evidence_wire']=VERSION
        return result

    def _payload(self,messages,tools):
        payload = dict(model=self.model, messages=messages, max_completion_tokens=self.max_completion_tokens)
        if self.enable_thinking is not None:
            payload['chat_template_kwargs'] = {'enable_thinking': self.enable_thinking}
        if self.temperature is not None:payload['temperature']=self.temperature
        if tools:
            payload.update(tools=tools, tool_choice='required' if self.structured_roles else 'auto')
            if self.structured_roles: payload['parallel_tool_calls']=False
        return payload

    def complete(self, messages, tools, *, role, case_id, turn):
        codec=None;wire=None
        try:
            payload=self._payload(messages,tools)
            if self.compact_evidence:
                from .wire import pack_request,request_stats
                logical=payload;payload,codec=pack_request(logical)
                wire=request_stats(logical,payload,codec)
            encoded=dumps(payload)
            encoded_bytes=encoded.encode('utf-8')
        except (ValueError,KeyError,TypeError,AttributeError,RecursionError):
            raise BackendError('invalid_wire_request' if self.compact_evidence else 'invalid_backend_request') from None
        request = Request(self.base_url + '/chat/completions', data=encoded_bytes,
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._api_key}, method='POST')
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise BackendError('response_byte_budget')
            data = loads(raw.decode('utf-8'))
            if not isinstance(data, dict) or not isinstance(data.get('choices'), list) or len(data['choices']) != 1:
                raise BackendError('invalid_choices')
            choice = data['choices'][0]
            if choice.get('finish_reason') not in {'stop', 'tool_calls'}:
                raise BackendError('incomplete_response')
            actual_model = data.get('model')
            if not isinstance(actual_model, str) or not actual_model:
                raise BackendError('missing_response_model')
            message=choice.get('message')
            if codec is not None:message=codec.decode_response(message)
            return Completion(_message(message), _usage(data.get('usage')), actual_model, wire)
        except HTTPError as error:
            error.close()
            raise BackendError('http_status_' + str(error.code)) from None
        except (URLError, HTTPException, socket.timeout, TimeoutError, OSError):
            raise BackendError('network_error') from None
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            raise BackendError('invalid_backend_response') from None


class ReplayBackend:
    def __init__(self, model, entries, capture_requests=True):
        if not isinstance(model, str) or not model.strip():
            raise ValueError('model required even for simulated replay')
        self.model, self.entries, self.requests = model, {}, []
        self.capture_requests = capture_requests
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {'role', 'case_id', 'turn', 'message', 'usage'}:
                raise ValueError('invalid replay entry')
            key = (entry.get('role'), entry.get('case_id'), entry.get('turn'))
            if (not all(isinstance(v, str) and v for v in key[:2]) or type(key[2]) is not int or key[2] < 0
                    or key in self.entries or 'message' not in entry):
                raise ValueError('invalid or duplicate replay key')
            self.entries[key] = loads(dumps(entry))

    def metadata(self):
        return dict(kind='replay', simulated=True, model=self.model)

    def complete(self, messages, tools, *, role, case_id, turn):
        if self.capture_requests:
            self.requests.append(loads(dumps(dict(role=role, case_id=case_id, turn=turn, messages=messages, tools=tools))))
        entry = self.entries.get((role, case_id, turn))
        if entry is None:
            raise BackendError('replay_missing')
        return Completion(_message(entry['message']), _usage(entry.get('usage')), self.model)

import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def backend():
    return importlib.import_module('aiops_v4.agents.backend')


@pytest.fixture
def server():
    captured = []
    reply = {'status': 200, 'payload': {'model': 'fixture-model', 'choices': [
        {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"ok":true}'}}],
        'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}}}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append((self.path, self.headers.get('Authorization'), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(reply['status'])
            if reply['status'] == 302:
                self.send_header('Location', '/redirected')
            self.end_headers()
            self.wfile.write(json.dumps(reply['payload']).encode())
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
    yield f'http://127.0.0.1:{http.server_port}/v1', captured, reply
    http.shutdown(); thread.join(); http.server_close()


def test_http_real_protocol_and_usage(server):
    url, captured, _ = server
    api = backend().HTTPBackend('fixture-model', url, 'test-secret', timeout=2)
    messages = [{'role': 'system', 'content': 'role'}, {'role': 'user', 'content': 'facts'}]
    tools = [{'type': 'function', 'function': {'name': 'query_states', 'parameters': {'type': 'object'}}}]
    result = api.complete(messages, tools, role='confirmation', case_id='case', turn=0)
    assert result.message['content'] == '{"ok":true}' and result.usage['total_tokens'] == 10
    assert result.model == 'fixture-model'
    path, authorization, request = captured[0]
    assert path == '/v1/chat/completions' and authorization == 'Bearer test-secret'
    assert request['model'] == 'fixture-model' and request['messages'] == messages
    assert request['tools'] == tools and request['tool_choice'] == 'auto'
    assert request['max_completion_tokens'] == 4096
    assert 'test-secret' not in repr(api) and 'test-secret' not in json.dumps(api.metadata())


@pytest.mark.parametrize('fault', ['length', 'refusal', 'bad_choice', 'duplicate_json', 'nonfinite', 'error', 'redirect', 'oversize'])
def test_http_failures_are_bounded_and_sanitized(server, fault):
    url, captured, reply = server
    if fault == 'length': reply['payload']['choices'][0]['finish_reason'] = 'length'
    if fault == 'refusal': reply['payload']['choices'][0]['message']['refusal'] = 'secret-server-text'
    if fault == 'bad_choice': reply['payload']['choices'] = []
    if fault == 'duplicate_json': reply['payload']['choices'][0]['message']['tool_calls'] = [{'id': 'x', 'type': 'function', 'function': {'name': 'query_states', 'arguments': '{"limit":1,"limit":2}'}}]
    if fault == 'nonfinite': reply['payload']['usage']['total_tokens'] = float('inf')
    if fault == 'error': reply.update(status=401, payload={'error': 'test-secret secret-server-text'})
    if fault == 'redirect': reply['status'] = 302
    if fault == 'oversize': reply['payload']['padding'] = 'x' * 10000
    api = backend().HTTPBackend('fixture-model', url, 'test-secret', timeout=2, max_response_bytes=4096)
    with pytest.raises(backend().BackendError) as exc:
        api.complete([], [], role='confirmation', case_id='case', turn=0)
    assert 'test-secret' not in str(exc.value) and 'secret-server-text' not in str(exc.value)
    assert len(captured) == 1


def test_http_disallows_remote_plaintext_and_credentials_in_url():
    for url in ['http://example.com/v1', 'https://user:secret@example.com/v1', 'https://example.com/v1?key=secret']:
        with pytest.raises(ValueError): backend().HTTPBackend('model', url, 'key')


def test_replay_is_explicit_and_exact_not_fallback():
    entry = dict(role='confirmation', case_id='bundle', turn=0, message={'role': 'assistant', 'content': '{"ok":true}'})
    api = backend().ReplayBackend('fixture-model', [entry])
    result = api.complete([], [], role='confirmation', case_id='bundle', turn=0)
    assert result.model == 'fixture-model' and api.metadata()['simulated'] is True
    assert api.requests[0]['role'] == 'confirmation'
    with pytest.raises(backend().BackendError, match='replay_missing'):
        api.complete([], [], role='classification', case_id='bundle', turn=0)
    with pytest.raises(ValueError): backend().ReplayBackend('fixture-model', [entry, entry])

import importlib
import json

import pytest
from test_evidence_pipeline import inputs, sha
from aiops_v4.evidence.pipeline import build_evidence
from aiops_v4.evidence.index import query_evidence


def tool_module():
    return importlib.import_module('aiops_v4.agents.tools')


@pytest.fixture
def evidence(tmp_path):
    _, _, states = inputs(tmp_path, interfaces=1)
    out = tmp_path / 'evidence'; build_evidence(states, 'stage2', out)
    db = out / 'evidence.sqlite'
    return db, query_evidence(db, 'stage2', 'bundle', entity_id='xian-br-1')[0]


def consume(session, reply):
    parts = [reply['text']]
    while reply['next_offset'] is not None:
        reply = session.call('read_chunk', {'handle': reply['handle'], 'offset': reply['next_offset']})
        parts.append(reply['text'])
    return json.loads(''.join(parts))


def test_seen_facts_are_only_fully_delivered_and_index_is_readonly(evidence):
    db, packet = evidence; before = sha(db)
    session = tool_module().EvidenceSession(db, 'stage2', packet, max_chunk_chars=256)
    reply = session.call('query_states', {'entity_id': 'xian-br-1', 'source': 'node', 'start': packet['start_time'], 'end': packet['end_time'], 'limit': 1})
    assert reply['next_offset'] and ('state', 'invented') not in session.seen
    rows = consume(session, reply)
    assert len(rows) == 1 and ('state', rows[0]['vector_id']) in session.seen
    assert rows[0]['matrix']['rule_inputs'] and rows[0]['scoring_provenance']
    assert sha(db) == before
    reply = session.call('query_model', {'group_id': rows[0]['group_id']})
    assert consume(session, reply)[0]['reference']['status'] == 'ready'


def test_raw_anchor_is_not_raw_evidence_until_full_text_delivered(evidence):
    db, packet = evidence
    session = tool_module().EvidenceSession(db, 'stage2', packet, max_chunk_chars=128)
    ref = packet['references'][0]
    assert ('raw', ref['record_id']) not in session.seen
    reply = session.call('query_raw', {'record_id': ref['record_id']})
    assert reply['next_offset'] and ('raw', ref['record_id']) not in session.seen
    full = consume(session, reply)
    assert full['raw'] and ('raw', ref['record_id']) in session.seen
    assert session.seen[('raw', ref['record_id'])]['entity_id'] == 'xian-br-1'
    with pytest.raises(ValueError): session.call('query_raw', {'record_id': 'invented'})


@pytest.mark.parametrize('name,args', [
    ('query_states', {'batch': 'stage1'}), ('query_raw', {'path': '/etc/passwd'}),
    ('query_members', {'bundle_id': 'foreign'}), ('query_model', {'group_id': 'unseen'}),
    ('query_states', {'limit': True}), ('query_states', {'start': '2026-09-17T04:00:00'}),
    ('delete', {}), ('read_chunk', {'handle': 'invented', 'offset': 0}),
])
def test_tool_parameters_reject_scope_and_path_injection(evidence, name, args):
    db, packet = evidence
    with pytest.raises(ValueError): tool_module().EvidenceSession(db, 'stage2', packet).call(name, args)


def test_chunks_cannot_skip_and_members_are_complete_paged(evidence):
    db, packet = evidence
    session = tool_module().EvidenceSession(db, 'stage2', packet, max_chunk_chars=128)
    reply = session.call('query_members', {'limit': 1})
    with pytest.raises(ValueError): session.call('read_chunk', {'handle': reply['handle'], 'offset': reply['next_offset'] + 1})
    first = consume(session, reply)
    rest = consume(session, session.call('query_members', {'limit': 100, 'offset': 1}))
    assert len(first + rest) == packet['event_count']
    assert all(('event', row['event_id']) in session.seen for row in first + rest)


def test_strict_json_disallows_duplicates_nonfinite_and_overflow():
    loader = importlib.import_module('aiops_v4.agents.jsonio').loads
    for text in ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}']:
        with pytest.raises(ValueError): loader(text)

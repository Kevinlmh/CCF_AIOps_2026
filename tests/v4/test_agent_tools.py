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


@pytest.mark.parametrize('name,args', [
    ('query_states', {'entity_id': 1}), ('query_states', {'source': []}),
    ('query_states', {'group_id': {}}), ('query_states', {'start': 123}),
    ('query_states', {'end': True}), ('query_states', {'start': None}),
    ('query_states', {'limit': False}), ('query_states', {'offset': True}),
    ('query_members', {'bundle_id': []}), ('query_members', {'limit': 0}),
    ('query_model', {'group_id': []}), ('query_relations', {'offset': -1}),
    ('query_raw', {'record_id': {}}), ('read_chunk', {'handle': [], 'offset': 0}),
])
def test_every_supplied_tool_scalar_has_runtime_type_validation(evidence, name, args):
    db, packet = evidence
    with pytest.raises(ValueError):
        tool_module().EvidenceSession(db, 'stage2', packet).call(name, args)


def test_paginated_relations_have_visible_stable_ids_and_delivered_only_citations(tmp_path):
    from aiops_v4.agents.contracts import citations
    _, _, states = inputs(tmp_path, interfaces=0)
    topology = tmp_path / 'topology.json'
    topology.write_text(json.dumps(dict(provenance='Explicit fixture, not official topology', edges=[
        dict(a='xian-br-1', b='xian-br-2', relation=f'fixture-edge-{i:02}') for i in range(12)])))
    out = tmp_path / 'evidence'; build_evidence(states, 'stage2', out, topology=topology)
    db = out / 'evidence.sqlite'
    packet = query_evidence(db, 'stage2', 'bundle', entity_id='xian-br-1')[0]
    assert packet['relation_count'] == 12 and packet['truncation']['relations']
    session = tool_module().EvidenceSession(db, 'stage2', packet, max_chunk_chars=128)
    initial = {row['relation']: row['relation_id'] for row in packet['relations']}
    reply = session.call('query_relations', {'limit': 1, 'offset': 8})
    assert reply['next_offset'] and len([key for key in session.seen if key[0] == 'relation']) == 8
    beyond = consume(session, reply)[0]
    assert beyond['relation_id'] and beyond['relation'] not in initial
    assert ('relation', beyond['relation_id']) in session.seen
    citations([dict(kind='relation', id=beyond['relation_id'], stance='context', claim='Direct overlap, no causal direction.')], session.seen)
    delivered = []
    for offset in range(0, 12, 3):
        delivered.extend(consume(session, session.call('query_relations', {'limit': 3, 'offset': offset})))
    assert len({row['relation_id'] for row in delivered}) == 12
    for row in delivered:
        if row['relation'] in initial:
            assert row['relation_id'] == initial[row['relation']]
        assert ('relation', row['relation_id']) in session.seen
    assert packet['relations'][0]['relation_id'] == initial[packet['relations'][0]['relation']]

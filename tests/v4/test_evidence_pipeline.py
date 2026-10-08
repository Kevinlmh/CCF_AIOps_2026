"""Use real window/state producers to test traceability and safe publication."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import pytest
from aiops_v4.features.pipeline import build_features
from aiops_v4.states.pipeline import discover_states
from test_records import write_csv
from test_evidence_bundles import module


def inputs(tmp_path, *, interfaces=35, spike=True):
    raw = tmp_path / 'raw'
    node, routing = [], []
    for role in ('br1', 'br2'):
        for i in range(15):
            time = f'2026-09-17T04:{i:02d}:00Z'
            anomalous = spike and i in (12, 13)
            node.append(dict(timestamp=time, node=role, cpu_usage='90' if anomalous else '10'))
            routing.append(dict(timestamp=time, node=role, metric_name='bgp_peer_up',
                                label='peer="192.0.2.1"', value='0' if anomalous else '1'))
    write_csv(raw, 'node_metrics.csv', node)
    write_csv(raw, 'routing_metrics.csv', routing)
    write_csv(raw, 'scrape_health.csv', [dict(timestamp='2026-09-17T04:12:00Z', node='br1',
                                            target_id='test:9100', exporter_type='node', scrape_up='1')])
    if interfaces:
        write_csv(raw, 'interface_metrics.csv', [dict(timestamp='2026-09-17T04:13:00Z', node='br1',
            interface_id=f'eth{i}', rx_drop_rate='1' if spike else '0') for i in range(interfaces)])
    windows, states = tmp_path / 'windows', tmp_path / 'states'
    build_features(raw, 'stage2', windows, naive_timezone='UTC')
    discover_states(windows, 'stage2', states, reference_start='2026-09-17T04:00:00Z', reference_end='2026-09-17T04:12:00Z')
    return raw, windows, states


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_producers_preserve_all_members_and_raw_anchors(tmp_path):
    raw, windows, states = inputs(tmp_path)
    out = tmp_path / 'evidence'
    summary = module('pipeline').build_evidence(states, 'stage2', out)
    assert summary['events'] == 39 and summary['bundles'] == 2
    db = out / 'evidence.sqlite'
    packets = module('index').query_evidence(db, 'stage2', 'bundle', entity_id='xian-br-1')
    assert len(packets) == 1
    packet = packets[0]
    assert packet['event_count'] == 37 and len(packet['event_ids']) == 32
    assert packet['event_ids_truncated']
    assert 'source_unavailable:frr' in packet['limitations']
    assert 'source_unavailable:traffic' in packet['limitations']
    assert all(s['scoring_status'] == 'scored' for s in packet['observations_without_current_trigger'])
    assert any(s['source'] == 'scrape' for s in packet['quality_context'])
    routing = next(s for s in packet['support'] if s['source'] == 'routing')
    assert routing['rule_inputs']['bgp_peer_up']['min'] == 0
    assert routing['dimensions']['peer'] == '192.0.2.1'
    members = module('index').query_evidence(db, 'stage2', 'members', bundle_id=packet['bundle_id'], limit=20)
    members += module('index').query_evidence(db, 'stage2', 'members', bundle_id=packet['bundle_id'], limit=20, offset=20)
    assert len(members) == len({m['event_id'] for m in members}) == 37
    selected = packet['references'][0]
    full = module('index').query_raw(db, 'stage2', selected['record_id'])
    assert full['record_id'] == selected['record_id'] and full['batch'] == 'stage2'
    assert full['raw'] and full['source_file'] == selected['source_file']
    before = sha(db)
    rows = module('index').query_evidence(db, 'stage2', 'states', entity_id='xian-br-1', source='node',
        start='2026-09-17T12:12:00+08:00', end='2026-09-17T12:13:00+08:00')
    assert len(rows) == 1 and rows[0]['candidate'] and rows[0]['matrix']['rule_inputs']
    assert module('index').query_evidence(db, 'stage2', 'model', group_id=rows[0]['group_id'])[0]['reference']['status'] == 'ready'
    assert sha(db) == before


def test_skip_raw_retrieves_on_demand_and_explicit_reference(tmp_path):
    raw, _, states = inputs(tmp_path, interfaces=0)
    out = tmp_path / 'evidence'
    summary = module('pipeline').build_evidence(states, 'stage2', out, skip_raw=True)
    assert summary['raw_records_materialized'] == 0
    db = out / 'evidence.sqlite'
    packet = module('index').query_evidence(db, 'stage2', 'bundle')[0]
    assert 'raw_not_materialized' in packet['limitations']
    ref = packet['references'][0]
    before = sha(db)
    record = module('index').query_raw(db, 'stage2', ref['record_id'])
    assert record['record_id'] == ref['record_id']
    assert module('index').query_raw(db, 'stage2', reference=ref)['raw'] == record['raw']
    assert sha(db) == before
    with pytest.raises(ValueError, match='unknown|registered'):
        module('index').query_raw(db, 'stage2', 'absent')


def test_topology_relates_direct_overlapping_bundles_only(tmp_path):
    _, _, states = inputs(tmp_path, interfaces=0)
    topology = tmp_path / 'topology.json'
    topology.write_text(json.dumps(dict(provenance='test fixture, not official diagram', edges=[
        dict(a='xian-br-1', b='xian-br-2', relation='link')])))
    out = tmp_path / 'evidence'
    module('pipeline').build_evidence(states, 'stage2', out, topology=topology)
    db = out / 'evidence.sqlite'
    packets = module('index').query_evidence(db, 'stage2', 'bundle')
    assert len(packets) == 2
    assert packets[0]['relations'][0]['other_bundle_id'] == packets[1]['bundle_id']
    links = module('index').query_evidence(db, 'stage2', 'relations', bundle_id=packets[0]['bundle_id'])
    assert len(links) == 1 and links[0]['provenance'].startswith('test fixture')
    assert links[0]['interpretation'] == 'direct_neighbor_overlap; no_causal_direction'


@pytest.mark.parametrize('fault', ['mixed', 'duplicate', 'bad_member', 'count', 'summary_link', 'window_link', 'matrix_alignment', 'missing_event'])
def test_bad_artifacts_publish_nothing(tmp_path, fault):
    _, windows, states = inputs(tmp_path, interfaces=0)
    path = states / 'events.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if fault == 'mixed': rows[0]['batch'] = 'stage1'
    if fault == 'duplicate': rows.append(rows[0])
    if fault == 'bad_member': rows[0]['window_ids'][0] = 'absent'
    if fault == 'missing_event': rows.pop()
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    if fault in ('count', 'summary_link'):
        path = states / 'summary.json'; s = json.loads(path.read_text())
        s['states' if fault == 'count' else 'input_summary_sha256'] = 999 if fault == 'count' else 'bad'
        path.write_text(json.dumps(s))
    if fault == 'window_link':
        with (windows / 'windows.jsonl').open('a') as f: f.write('{}\n')
    if fault == 'matrix_alignment':
        path = states / 'matrix.jsonl'; rows = [json.loads(l) for l in path.read_text().splitlines()]
        rows[0]['dimensions'] = {'bad': 'dimension'}
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    before = {p: sha(p) for parent in (states, windows) for p in parent.iterdir()}
    out = tmp_path / 'evidence'
    with pytest.raises(ValueError): module('pipeline').build_evidence(states, 'stage2', out, skip_raw=True)
    assert not out.exists()
    assert all(sha(p) == digest for p, digest in before.items())


def test_output_protection_failed_raw_publication_and_wrong_queries(tmp_path):
    raw, windows, states = inputs(tmp_path, interfaces=0)
    out = tmp_path / 'evidence'; out.mkdir(); (out / 'keep').write_text('original')
    with pytest.raises(FileExistsError): module('pipeline').build_evidence(states, 'stage2', out)
    assert (out / 'keep').read_text() == 'original'
    for parent in (raw, windows, states):
        with pytest.raises(ValueError): module('pipeline').build_evidence(states, 'stage2', parent / 'nested')
    path = next(raw.rglob('node_metrics.csv'))
    path.write_text(path.read_text().replace(',90', ',91'))
    with pytest.raises(ValueError): module('pipeline').build_evidence(states, 'stage2', tmp_path / 'failed')
    assert not (tmp_path / 'failed').exists()
    module('pipeline').build_evidence(states, 'stage2', tmp_path / 'skip', skip_raw=True)
    db = tmp_path / 'skip' / 'evidence.sqlite'
    for kwargs in [{'batch': 'stage1'}, {'batch': 'stage2', 'limit': 0}, {'batch': 'stage2', 'offset': -1},
                   {'batch': 'stage2', 'start': '2026-09-17T04:00:00'}]:
        with pytest.raises(ValueError): module('index').query_evidence(db, kind='bundle', **kwargs)


def test_no_candidates_is_a_valid_empty_review_queue(tmp_path):
    _, _, states = inputs(tmp_path, interfaces=0, spike=False)
    out = tmp_path / 'evidence'
    summary = module('pipeline').build_evidence(states, 'stage2', out)
    assert summary['events'] == summary['bundles'] == summary['raw_records_materialized'] == 0
    assert module('index').query_evidence(out / 'evidence.sqlite', 'stage2', 'bundle') == []


def test_cli_outside_checkout_and_read_only_connection(tmp_path):
    _, _, states = inputs(tmp_path, interfaces=0)
    out = tmp_path / 'evidence'
    p = subprocess.run([sys.executable, '-m', 'aiops_v4.evidence', 'build', '--states-dir', str(states),
                        '--batch', 'stage2', '--output-dir', str(out), '--skip-raw'], cwd=tmp_path, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['bundles'] == 2
    db = out / 'evidence.sqlite'; before = sha(db)
    p = subprocess.run([sys.executable, '-m', 'aiops_v4.evidence', 'query', '--database', str(db),
                        '--batch', 'stage2', '--kind', 'bundle', '--limit', '1'], cwd=tmp_path, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert len(json.loads(p.stdout)) == 1 and sha(db) == before
    with module('index').read_index(db, 'stage2') as connection:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            connection.execute("DELETE FROM bundles")


def test_prefix_scope_and_long_frr_survive_complete_pipeline(tmp_path):
    raw = tmp_path / 'raw'
    message = 'error detail\n' + '诊断' * 100000
    write_csv(raw, 'frr_syslog_events.csv', [
        dict(event_time='2026-09-17 12:00:01', hostname='br1', severity='error', program='bgpd', message=message),
        dict(event_time='2026-09-17 12:01:01', hostname='br1', severity='error', program='bgpd', message='out of scope')])
    windows, states, out = (tmp_path / name for name in ('windows', 'states', 'evidence'))
    build_features(raw, 'stage2', windows, naive_timezone='Asia/Shanghai', max_rows_per_file=1)
    discover_states(windows, 'stage2', states)
    summary = module('pipeline').build_evidence(states, 'stage2', out)
    assert summary['input_scope']['mode'] == 'prefix_sample' and summary['input_scope']['complete'] is False
    packet = module('index').query_evidence(out / 'evidence.sqlite', 'stage2', 'bundle')[0]
    assert 'prefix_sample' in packet['limitations']
    assert packet['start_time'] == '2026-09-17T04:00:00.000000Z'
    record = module('index').query_raw(out / 'evidence.sqlite', 'stage2', packet['references'][0]['record_id'])
    assert record['raw']['message'] == message and record['observed_time'] == '2026-09-17T04:00:01.000000Z'
    assert (record['line_start'], record['line_end']) == (2, 3)


def test_publish_failure_rolls_back_only_owned_artifacts(tmp_path, monkeypatch):
    _, _, states = inputs(tmp_path, interfaces=0)
    pipeline = module('pipeline')
    out = tmp_path / 'evidence'
    real_link = pipeline.os.link
    def failing_link(source, destination):
        if Path(destination).name == 'summary.json':
            # A concurrent user's file must survive rollback.
            (out / 'keep').write_text('user-owned')
            raise OSError('simulated publication failure')
        return real_link(source, destination)
    monkeypatch.setattr(pipeline.os, 'link', failing_link)
    with pytest.raises(OSError, match='publication'):
        pipeline.build_evidence(states, 'stage2', out, skip_raw=True)
    assert [p.name for p in out.iterdir()] == ['keep']
    assert (out / 'keep').read_text() == 'user-owned'


def test_direct_topology_touching_intervals_do_not_create_relation(tmp_path):
    raw = tmp_path / 'raw'
    write_csv(raw, 'scrape_health.csv', [
        dict(timestamp='2026-09-17T04:00:00Z', node='br1', target_id='a', scrape_up='0'),
        dict(timestamp='2026-09-17T04:01:00Z', node='br2', target_id='b', scrape_up='0')])
    windows, states, out = (tmp_path / name for name in ('windows', 'states', 'evidence'))
    build_features(raw, 'stage2', windows, naive_timezone='UTC'); discover_states(windows, 'stage2', states)
    topology = tmp_path / 'topology.json'
    topology.write_text(json.dumps(dict(provenance='test', edges=[dict(a='xian-br-1', b='xian-br-2', relation='link')])))
    summary = module('pipeline').build_evidence(states, 'stage2', out, topology=topology)
    assert summary['bundles'] == 2 and summary['relations'] == 0

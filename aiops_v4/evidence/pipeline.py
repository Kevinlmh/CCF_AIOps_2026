"""Validate native artifacts and publish a disk-backed, traceable review queue."""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
from itertools import zip_longest
import os
from pathlib import Path
import sqlite3
import tempfile
from aiops_v4.data.reader import utc_time
from aiops_v4.features.time import iso_time, time_zone
from aiops_v4.features.series import VIEWS
from aiops_v4.states.pipeline import _load, _dump
from aiops_v4.states.matrix import window_vector
from .association import iter_bundles, load_topology
from .index import create_index, put_metadata, scoring_provenance
from .packet import compact_observation, make_packet
from .raw import resolve_references, validate_reference

ARTIFACTS = ('bundles.jsonl', 'evidence.sqlite', 'report.md', 'summary.json')
GRAIN = ('vector_id', 'group_id', 'batch', 'source', 'view', 'identity', 'dimensions', 'start_time', 'end_time', 'references', 'references_truncated')


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(path, digests):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for number, line in enumerate(f, 1):
            digest.update(line)
            try:
                row = _load(line)
                if not isinstance(row, dict):
                    raise ValueError('object required')
                yield row
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f'invalid {path.name} line {number}: {error}') from error
    digests[path.name] = digest.hexdigest()


def _interval(row):
    start = utc_time(row['start_time'], require_timezone=True)[0]
    end = utc_time(row['end_time'], require_timezone=True)[0]
    if start >= end or row['start_time'] != start or row['end_time'] != end:
        raise ValueError('canonical increasing UTC interval required')
    return start, end


def _add_anchor(db, ref, manifest):
    ref = validate_reference(ref, manifest)
    payload = _dump(ref)
    old = db.execute('SELECT payload FROM anchors WHERE id=?', (ref['record_id'],)).fetchone()
    if old is not None and old[0] != payload:
        raise ValueError('conflicting raw anchor provenance')
    if old is None:
        db.execute('INSERT INTO anchors VALUES (?,?,?,?)', (ref['record_id'], ref['source_file'], ref['record_index'], payload))


def _import(db, root, windows, batch, state_summary, window_summary):
    counts, sources, hashes = Counter(), Counter(), {}
    manifest = {entry['path']: entry for entry in window_summary['files']}
    if len(manifest) != len(window_summary['files']):
        raise ValueError('duplicate raw manifest file')
    # The summary hash identifies the linked window artifact. Independently derive
    # its vectors so a genuine summary from a different run cannot bless old files.
    db.execute('CREATE TEMP TABLE linked_vectors(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    window_hashes = {}
    for window in _rows(windows / 'windows.jsonl', window_hashes):
        vector = window_vector(window, batch)
        db.execute('INSERT INTO linked_vectors VALUES (?,?)', (vector['vector_id'], _dump(vector)))
    if window_hashes['windows.jsonl'] != state_summary['input_windows_sha256']:
        raise ValueError('linked windows changed during import')
    for model in _rows(root / 'models.jsonl', hashes):
        group = model['group_id']
        reference = model['reference']
        empty_reference = reference['group_id'] is None and reference['observed_reference_rows'] == 0 and reference['status'] == 'insufficient_reference'
        if not isinstance(group, str) or not group or (reference['group_id'] != group and not empty_reference):
            raise ValueError('model group mismatch')
        db.execute('INSERT INTO models VALUES (?,?)', (group, _dump(model)))
        counts['groups'] += 1
        counts['ready_models'] += model['reference']['status'] == 'ready'
    states = _rows(root / 'states.jsonl', hashes)
    matrices = _rows(root / 'matrix.jsonl', hashes)
    last_by_group = None
    for state, matrix in zip_longest(states, matrices):
        if state is None or matrix is None or any(state[k] != matrix[k] for k in GRAIN):
            raise ValueError('state/matrix count or native grain alignment mismatch')
        start, end = _interval(state)
        if state.get('schema_version') != 1 or matrix.get('schema_version') != 1 or state['batch'] != batch:
            raise ValueError('state/matrix batch or schema mismatch')
        expected = db.execute('SELECT payload FROM linked_vectors WHERE id=?', (matrix['vector_id'],)).fetchone()
        derived = {k: v for k, v in matrix.items() if k != 'in_reference'}
        scope = state_summary['reference_scope']
        in_reference = scope['mode'] == 'offline_same_batch' or (scope['start'] <= start and end <= scope['end'])
        if expected is None or expected[0] != _dump(derived) or matrix.get('in_reference') is not in_reference or state['is_reference'] is not in_reference:
            raise ValueError('imported matrix/state does not match linked window and reference scope')
        if state['source'] not in VIEWS or state['view'] != VIEWS[state['source']] or type(state['candidate']) is not bool:
            raise ValueError('invalid native source/view/candidate')
        entity, group = state['identity']['entity_id'], state['group_id']
        if not isinstance(entity, str) or not entity or not isinstance(group, str) or not isinstance(state['dimensions'], dict):
            raise ValueError('native identity/group/dimensions required')
        key = group, start, state['vector_id']
        if last_by_group is not None and (key < last_by_group[0] or (group == last_by_group[0][0] and start < last_by_group[1])):
            raise ValueError('state series order or interval overlap mismatch')
        last_by_group = key, end
        if state['scoring_status'] not in {'scored', 'blocked_quality', 'insufficient_reference', 'insufficient_feature_overlap'}:
            raise ValueError('unknown scoring status')
        for ref in state['references']:
            _add_anchor(db, ref, manifest)
        quality = bool(state['quality_flags'] or state['quality_signals'] or state['scoring_status'] != 'scored')
        db.execute('INSERT INTO states VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (state['vector_id'], entity, group, state['source'], start, end, state['candidate'], state['scoring_status'], quality, _dump(state)))
        db.execute('INSERT INTO matrices VALUES (?,?)', (state['vector_id'], _dump(matrix)))
        counts['states'] += 1
        counts['candidate_windows'] += state['candidate']
        sources[state['source']] += 1
    for event in _rows(root / 'events.jsonl', hashes):
        start, end = _interval(event)
        if event.get('schema_version') != 1 or event['batch'] != batch or event.get('needs_confirmation') is not True:
            raise ValueError('event schema/batch/confirmation mismatch')
        if not isinstance(event['references'], list) or len(event['references']) > 16:
            raise ValueError('event references exceed producer capacity')
        requested_refs = {ref['record_id'] for ref in event['references']}
        member_refs = set()
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                   (event['event_id'], event['identity']['entity_id'], event['group_id'], start, end, _dump(event)))
        member_count, ids, prior_end = 0, [], None
        for payload, in db.execute('SELECT payload FROM states WHERE grp=? AND start>=? AND end<=? ORDER BY start,id',
                                  (event['group_id'], start, end)):
            state = _load(payload)
            if not state['candidate'] or any(state[k] != event[k] for k in ('identity', 'dimensions', 'source', 'view')):
                raise ValueError('event includes noncandidate or different native grain')
            if (prior_end is None and state['start_time'] != start) or (prior_end is not None and state['start_time'] != prior_end):
                raise ValueError('event member windows must be contiguous')
            prior_end = state['end_time']
            member_refs.update(ref['record_id'] for ref in state['references'] if ref['record_id'] in requested_refs)
            member_count += 1
            if len(ids) < 64:
                ids.append(state['vector_id'])
            db.execute('INSERT INTO event_windows VALUES (?,?)', (state['vector_id'], event['event_id']))
        if not member_count or prior_end != end or event['window_count'] != member_count or event['window_ids'] != ids or event['window_ids_truncated'] != (member_count > 64):
            raise ValueError('event member count/IDs/interval mismatch')
        for ref in event['references']:
            canonical = validate_reference(ref, manifest)
            old = db.execute('SELECT payload FROM anchors WHERE id=?', (ref['record_id'],)).fetchone()
            if old is None or old[0] != _dump(canonical):
                raise ValueError('event reference missing from indexed states')
            if ref['record_id'] not in member_refs:
                raise ValueError('event reference ownership does not match its member windows')
        counts['events'] += 1
    for local, expected in [('states', 'states'), ('groups', 'groups'), ('ready_models', 'ready_models'),
                            ('candidate_windows', 'candidate_windows'), ('events', 'candidate_events')]:
        if type(state_summary.get(expected)) is not int or counts[local] != state_summary[expected]:
            raise ValueError('state summary count mismatch: ' + expected)
    if sources != state_summary['states_by_source'] or counts['states'] != window_summary['window_count']:
        raise ValueError('source/window summary count mismatch')
    if db.execute('SELECT count(*) FROM event_windows').fetchone()[0] != counts['candidate_windows']:
        raise ValueError('not every candidate state has exactly one event')
    if db.execute('SELECT count(DISTINCT grp) FROM states').fetchone()[0] != counts['groups']:
        raise ValueError('orphan model or missing group')
    if db.execute('SELECT count(*) FROM linked_vectors').fetchone()[0] != counts['states']:
        raise ValueError('linked window/matrix count mismatch')
    db.execute('DROP TABLE linked_vectors')
    return counts, hashes


def _associate(db, batch):
    rows = (_load(p) for p, in db.execute('SELECT payload FROM events ORDER BY entity,start,end,id'))
    count = 0
    for bundle in iter_bundles(rows, batch):
        db.execute('INSERT INTO bundles VALUES (?,?,?,?,?)', (bundle['bundle_id'], bundle['entity_id'], bundle['start_time'], bundle['end_time'], _dump(bundle)))
        # Each connected component has an uninterrupted union. Its envelope selects
        # exactly its original events; no unbounded member list is held in memory.
        db.execute('INSERT INTO members SELECT id,? FROM events WHERE entity=? AND start<? AND end>?',
                   (bundle['bundle_id'], bundle['entity_id'], bundle['end_time'], bundle['start_time']))
        if db.execute('SELECT count(*) FROM members WHERE bundle=?', (bundle['bundle_id'],)).fetchone()[0] != bundle['event_count']:
            raise ValueError('bundle membership mismatch')
        count += 1
    if db.execute('SELECT count(*) FROM members').fetchone()[0] != db.execute('SELECT count(*) FROM events').fetchone()[0]:
        raise ValueError('event membership conservation failed')
    return count


def _relations(db, topology):
    for edge in topology['edges']:
        for anchor, neighbor in ((edge['a'], edge['b']), (edge['b'], edge['a'])):
            query = '''SELECT a.id,b.id,b.start,b.end FROM bundles a JOIN bundles b
                       ON a.start<b.end AND b.start<a.end WHERE a.entity=? AND b.entity=? ORDER BY a.start,a.id,b.start,b.id'''
            for a, b, start, end in db.execute(query, (anchor, neighbor)):
                payload = dict(bundle_id=a, other_bundle_id=b, other_entity_id=neighbor, start_time=start, end_time=end,
                               relation=edge['relation'], provenance=topology['provenance'],
                               interpretation='direct_neighbor_overlap; no_causal_direction')
                db.execute('INSERT INTO relations VALUES (?,?,?,?)', (a, b, edge['relation'], _dump(payload)))


def _context(db, bundle, seconds, cap, state_summary, window_summary, topology, skip_raw):
    start = iso_time(datetime.fromisoformat(bundle['start_time'].replace('Z', '+00:00')) - timedelta(seconds=seconds))
    end = iso_time(datetime.fromisoformat(bundle['end_time'].replace('Z', '+00:00')) + timedelta(seconds=seconds))
    scopes = {
        'support': ('''s.id IN (SELECT w.id FROM event_windows w JOIN members m ON m.event=w.event WHERE m.bundle=?)''', [bundle['bundle_id']]),
        'observations_without_current_trigger': ('s.entity=? AND s.start<? AND s.end>? AND s.candidate=0 AND s.status=\'scored\'', [bundle['entity_id'], end, start]),
        'quality_context': ('s.entity=? AND s.start<? AND s.end>? AND s.quality=1', [bundle['entity_id'], end, start])}
    observations, totals = {}, {}
    for kind, (condition, args) in scopes.items():
        totals[kind] = db.execute('SELECT count(*) FROM states s WHERE ' + condition, args).fetchone()[0]
        query = 'SELECT s.payload,x.payload FROM states s JOIN matrices x ON x.id=s.id WHERE ' + condition + ' ORDER BY s.start,s.source,s.grp,s.id LIMIT ?'
        observations[kind] = [compact_observation(_load(a), _load(b)) for a, b in db.execute(query, (*args, cap))]
    availability = {source: dict(observed=0, scorable=0, candidates=0) for source in window_summary['sources']}
    for source, observed, scorable, candidates in db.execute('''SELECT source,count(*),sum(status='scored'),sum(candidate)
          FROM states WHERE entity=? AND start<? AND end>? GROUP BY source''', (bundle['entity_id'], end, start)):
        availability[source] = dict(observed=observed, scorable=scorable, candidates=candidates)
    totals['relations'] = db.execute('SELECT count(*) FROM relations WHERE bundle=?', (bundle['bundle_id'],)).fetchone()[0]
    relations = [_load(p) for p, in db.execute('SELECT payload FROM relations WHERE bundle=? ORDER BY other,relation LIMIT 8', (bundle['bundle_id'],))]
    source_metadata = dict(sources=window_summary['sources'], local_availability=availability,
        scope={k: window_summary[k] for k in ('mode', 'complete', 'max_rows_per_file', 'naive_timezone', 'timezone_officially_confirmed')},
        context_start=start, context_end=end, raw_materialized=not skip_raw,
        topology_available=topology['available'], topology_provenance=topology['provenance'],
        selection='earliest_time_then_source_and_native_group; bounded_examples_not_exhaustive')
    packet = make_packet(bundle, observations['support'], observations['observations_without_current_trigger'],
                         observations['quality_context'], source_metadata, relations, dict(observations_per_kind=cap, totals=totals))
    packet['scoring_provenance'] = scoring_provenance(state_summary)
    return packet


def _publish(scratch, output):
    output.mkdir(exist_ok=False)
    linked = []
    try:
        for name in ARTIFACTS:
            os.link(scratch / name, output / name)
            linked.append(name)
    except OSError:
        for name in linked:
            path = output / name
            if path.is_file() and os.path.samestat(path.stat(), (scratch / name).stat()):
                path.unlink()
        try:
            output.rmdir()
        except OSError:
            pass
        raise


def build_evidence(states_dir, batch, output_dir, *, raw_root=None, skip_raw=False, topology=None,
                   context_seconds=120, observations_per_kind=8):
    root, output = Path(states_dir).resolve(), Path(output_dir)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'output directory already exists: {output}')
    if not isinstance(batch, str) or not batch.strip() or type(skip_raw) is not bool:
        raise ValueError('nonempty batch and boolean skip_raw required')
    if type(context_seconds) is not int or not 0 <= context_seconds <= 86400 or type(observations_per_kind) is not int or not 1 <= observations_per_kind <= 32:
        raise ValueError('context_seconds must be 0..86400; observations_per_kind must be 1..32')
    state_bytes = (root / 'summary.json').read_bytes()
    state_summary = _load(state_bytes)
    if state_summary.get('schema_version') != 1 or state_summary.get('batch') != batch:
        raise ValueError('state summary schema/batch mismatch')
    windows = Path(state_summary['windows_dir']).resolve()
    window_bytes = (windows / 'summary.json').read_bytes()
    window_summary = _load(window_bytes)
    if window_summary.get('schema_version') != 1 or window_summary.get('batch') != batch:
        raise ValueError('window summary schema/batch mismatch')
    if hashlib.sha256(window_bytes).hexdigest() != state_summary['input_summary_sha256'] or _sha(windows / 'windows.jsonl') != state_summary['input_windows_sha256']:
        raise ValueError('upstream summary/windows hash link mismatch')
    if any(state_summary['input_scope'].get(k) != window_summary.get(k) for k in state_summary['input_scope']):
        raise ValueError('upstream scope metadata mismatch')
    scope = state_summary['reference_scope']
    if scope.get('known_healthy') is not False or scope.get('mode') not in {'explicit_interval', 'offline_same_batch'}:
        raise ValueError('invalid reference scope')
    if scope['mode'] == 'explicit_interval':
        _interval(dict(start_time=scope['start'], end_time=scope['end']))
    elif scope.get('start') is not None or scope.get('end') is not None:
        raise ValueError('offline reference scope must not have interval bounds')
    raw = Path(raw_root if raw_root is not None else window_summary['input_root']).resolve()
    output = output.resolve()
    inputs = (root, windows, raw, Path(window_summary['input_root']).resolve())
    if any(output == p or p in output.parents for p in inputs):
        raise ValueError('output must be outside all state/window/raw input directories')
    time_zone(window_summary['naive_timezone'])
    topology = load_topology(topology)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.v4-evidence-', dir=output.parent) as temporary:
        scratch = Path(temporary)
        db = create_index(scratch / 'evidence.sqlite')
        try:
            counts, hashes = _import(db, root, windows, batch, state_summary, window_summary)
            counts['bundles'] = _associate(db, batch)
            _relations(db, topology)
            for key, value in dict(batch=batch, raw_root=str(raw), raw_manifest=window_summary['files'],
                                    naive_timezone=window_summary['naive_timezone'], state_summary=state_summary,
                                    window_summary=window_summary, topology=topology).items():
                put_metadata(db, key, value)
            with (scratch / 'bundles.jsonl').open('w', encoding='utf-8') as stream:
                for bundle_id, payload in db.execute('SELECT id,payload FROM bundles ORDER BY entity,start,id'):
                    packet = _context(db, _load(payload), context_seconds, observations_per_kind, state_summary, window_summary, topology, skip_raw)
                    stream.write(_dump(packet) + '\n')
                    db.execute('UPDATE bundles SET payload=? WHERE id=?', (_dump(packet), bundle_id))
                    for ref in packet['references']:
                        db.execute('INSERT OR IGNORE INTO wanted_raw VALUES (?)', (ref['record_id'],))
            if not skip_raw:
                refs = (_load(p) for p, in db.execute('SELECT a.payload FROM anchors a JOIN wanted_raw w ON w.id=a.id'))
                for record in resolve_references(raw, batch, refs, window_summary['files'], window_summary['naive_timezone']):
                    db.execute('INSERT INTO raw_records VALUES (?,?)', (record['record_id'], _dump(record)))
                    counts['raw_records_materialized'] += 1
            counts['packet_raw_references'] = db.execute('SELECT count(*) FROM wanted_raw').fetchone()[0]
            counts['registered_raw_references'] = db.execute('SELECT count(*) FROM anchors').fetchone()[0]
            counts['relations'] = db.execute('SELECT count(*) FROM relations').fetchone()[0]
            if not skip_raw and counts['raw_records_materialized'] != counts['packet_raw_references']:
                raise ValueError('raw reference materialization count mismatch')
            db.commit()
        except (KeyError, TypeError, AttributeError, sqlite3.IntegrityError) as error:
            raise ValueError(f'invalid/inconsistent evidence input: {error}') from error
        finally:
            db.close()
        summary = dict(schema_version=2, batch=batch, states_dir=str(root), windows_dir=str(windows), raw_root=str(raw),
                       output_dir=str(output), input_state_summary_sha256=hashlib.sha256(state_bytes).hexdigest(),
                       input_artifact_sha256=hashes, input_windows_sha256=state_summary['input_windows_sha256'],
                       input_scope=state_summary['input_scope'], reference_scope=state_summary['reference_scope'],
                       topology=topology, config=dict(context_seconds=context_seconds, observations_per_kind=observations_per_kind, skip_raw=skip_raw),
                       capacities=dict(event_ids=32, raw_references=32, relations=8),
                       diagnoses_confirmed=False, raw_scope='selected_packet_anchors_only',
                       **{k: counts[k] for k in ('states', 'groups', 'candidate_windows', 'events', 'bundles', 'relations',
                            'packet_raw_references', 'registered_raw_references', 'raw_records_materialized')})
        summary['artifact_sha256'] = {name: _sha(scratch / name) for name in ('bundles.jsonl', 'evidence.sqlite')}
        report = '\n'.join(['# v4 审阅证据报告', '', f"批次：{batch}；模式：{summary['input_scope']['mode']}。",
            f"状态 {counts['states']}；原事件 {counts['events']}；审阅包 {counts['bundles']}；物化原始引用 {counts['raw_records_materialized']}。", '',
            '原事件恰好进入一个包，完整成员和 native grain 可分页查询。严格重叠只提供审阅关联，包络不是确认故障时段。',
            '未触发观测仅来自可评分窗口，不能视作已知正常；无记录、不可评分及未知单位显式保留。',
            '数据库保存所有输入状态/矩阵/模型，原始记录仅物化包内引用；其他已登记锚点可按需核验读取。',
            '包内有界例子按时间和来源排序，完整线索应通过 states/members 查询补取。',
            f"拓扑可用：{topology['available']}；未提供连线时不会猜测传播路径。", '',
            '继承前缀、时间、单位和参考范围限制；LLM 确认、定位、分类、复核与官方导出仍待后续阶段。', ''])
        (scratch / 'report.md').write_text(report, encoding='utf-8')
        (scratch / 'summary.json').write_text(_dump(summary) + '\n', encoding='utf-8')
        _publish(scratch, output)
    return summary

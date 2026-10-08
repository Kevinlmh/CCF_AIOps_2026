"""Disk-backed within-batch state discovery; no labels or old caches consumed."""
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from aiops_v4.data.reader import utc_time
from .matrix import window_vector
from .reference import fit_reference, normalize
from .cluster import fit_clusters
from .signals import assess
from .events import EventBuilder

ARTIFACTS = ('matrix.jsonl', 'models.jsonl', 'states.jsonl', 'events.jsonl', 'report.md', 'summary.json')


def _bad_constant(value):
    raise ValueError(f'nonfinite JSON value: {value}')


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f'nonfinite JSON number: {value}')
    return parsed


def _load(value):
    return json.loads(value, parse_constant=_bad_constant, parse_float=_finite_float)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _publish(scratch, output):
    output.mkdir(exist_ok=False)
    published = []
    try:
        for name in ARTIFACTS:
            os.link(scratch / name, output / name)
            published.append(name)
    except OSError:
        for name in published:
            destination = output / name
            if destination.is_file() and os.path.samestat(destination.stat(), (scratch / name).stat()):
                destination.unlink()
        try:
            output.rmdir()
        except OSError:
            pass
        raise


def _report(summary):
    scope, ref = summary['input_scope'], summary['reference_scope']
    return '\n'.join([
        '# v4 状态发现报告', '', f"批次：{summary['batch']}；输入模式：{scope['mode']}；完整扫描：{scope['complete']}。",
        f"向量/状态 {summary['states']}；原始序列 {summary['groups']}；可用参考模型 {summary['ready_models']}；候选事件 {summary['candidate_events']}。", '',
        f"参考模式：{ref['mode']}；区间：{ref['start']} 至 {ref['end']}，窗口完整包含、终点不包含；known_healthy=false。",
        '未指定区间时是同批次离线无监督拟合，不能作为在线因果评估或已知正常基线。', '',
        '数值中位数/MAD、IQR 和常量 floor 是实验参考尺度；簇号没有正常/故障标签。',
        '参考向量最多64个；每特征分位数样本最多256个；簇占比是参考样本占比。',
        '特征缺失不补零；不足参考/共同特征明确保留状态。未知语义不参与关键数值判断。',
        '规则输出观测事实，计数器质量问题单列。正常窗口或时间空缺终止候选事件。',
        '事件引用上限16、window_id上限64，截断标记明确；没有事件数量或最长时长限制。', '',
        '## 可用性', '', '| 状态 | 窗口 |', '|---|---:|',
        *[f'| {key} | {count} |' for key, count in sorted(summary['scoring_status_counts'].items())], '',
        '## 范围限制', '',
        '继承输入的采样、时区和语义核验状态。前缀样本不能代表全批次。',
        '候选事件数量不是故障数量；未实现 LLM 事件确认、根因定位和故障分类。',
        '完整配置、输入摘要、触发来源及不可用参考数量见 summary.json。', ''])


def discover_states(windows_dir, batch, output_dir, *, reference_start=None, reference_end=None,
                    min_reference=12, clusters=3, mode='hybrid', stat_threshold=6.0,
                    cluster_threshold=3.0, rare_fraction=0.1, minimum_overlap=0.5,
                    rules_enabled=True, progress=None):
    root, output = Path(windows_dir).resolve(), Path(output_dir)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'output directory already exists: {output}')
    output = output.resolve()
    if root == output or root in output.parents:
        raise ValueError('output directory must be outside input directory')
    if not isinstance(batch, str) or not batch.strip() or mode not in {'statistics', 'cluster', 'hybrid'}:
        raise ValueError('non-empty batch and valid mode required')
    if type(min_reference) is not int or not 2 <= min_reference <= 256 or type(clusters) is not int or not 1 <= clusters <= 8:
        raise ValueError('min_reference must be 2..256 and clusters 1..8')
    for name, value in [('stat_threshold', stat_threshold), ('cluster_threshold', cluster_threshold), ('minimum_overlap', minimum_overlap)]:
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be finite and positive')
    if minimum_overlap > 1 or not isinstance(rare_fraction, (int, float)) or not math.isfinite(rare_fraction) or not 0 <= rare_fraction <= 1 or type(rules_enabled) is not bool:
        raise ValueError('invalid overlap, rarity fraction or rules switch')
    if (reference_start is None) != (reference_end is None):
        raise ValueError('reference_start and reference_end must be supplied together')
    if reference_start is not None:
        reference_start = utc_time(reference_start, require_timezone=True)[0]
        reference_end = utc_time(reference_end, require_timezone=True)[0]
        if reference_start >= reference_end:
            raise ValueError('reference interval must be increasing')
    summary_bytes = (root / 'summary.json').read_bytes()
    input_summary = _load(summary_bytes)
    if input_summary.get('batch') != batch or input_summary.get('schema_version') != 1 or input_summary.get('mode') not in {'full', 'prefix_sample'}:
        raise ValueError('input summary batch/schema/scope mismatch')
    if type(input_summary.get('window_count')) is not int or type(input_summary.get('complete')) is not bool:
        raise ValueError('input summary count/completeness required')
    thresholds = dict(stat_threshold=stat_threshold, cluster_threshold=cluster_threshold,
                      rare_fraction=rare_fraction, minimum_overlap=minimum_overlap, rules_enabled=rules_enabled)
    counts, statuses, trigger_counts, source_counts = Counter(), Counter(), Counter(), Counter()
    digest = hashlib.sha256()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.v4-state-', dir=output.parent) as temporary:
        scratch = Path(temporary)
        connection = sqlite3.connect(scratch / 'matrix.sqlite')
        try:
            connection.execute('PRAGMA temp_store=FILE')
            connection.execute('PRAGMA cache_size=-8192')
            connection.execute('CREATE TABLE vectors (id TEXT PRIMARY KEY, grp TEXT, start TEXT, end TEXT, payload TEXT)')
            pending = []
            with (root / 'windows.jsonl').open('rb') as source:
                for number, line in enumerate(source, 1):
                    digest.update(line)
                    try:
                        row = window_vector(_load(line), batch)
                    except (KeyError, TypeError, AttributeError, ValueError) as error:
                        raise ValueError(f'invalid window at line {number}: {error}') from error
                    pending.append((row['vector_id'], row['group_id'], row['start_time'], row['end_time'], _dump(row)))
                    counts['states'] += 1
                    if len(pending) >= 128:
                        connection.executemany('INSERT INTO vectors VALUES (?,?,?,?,?)', pending)
                        pending.clear()
                    if progress and counts['states'] % 10000 == 0:
                        progress({'event': 'matrix', 'rows': counts['states']})
            connection.executemany('INSERT INTO vectors VALUES (?,?,?,?,?)', pending)
            connection.execute('CREATE INDEX group_time ON vectors(grp,start,id)')
            connection.commit()
            if counts['states'] == 0 or counts['states'] != input_summary['window_count']:
                raise ValueError('input window count mismatch or empty input')
            def rows_for(group, reference=False):
                query, args = 'SELECT payload FROM vectors WHERE grp=?', [group]
                if reference and reference_start is not None:
                    query += ' AND start>=? AND end<=?'
                    args += [reference_start, reference_end]
                for (payload,) in connection.execute(query + ' ORDER BY start,id', args):
                    yield _load(payload)
            with (scratch / 'matrix.jsonl').open('w', encoding='utf-8') as matrices, \
                 (scratch / 'models.jsonl').open('w', encoding='utf-8') as models, \
                 (scratch / 'states.jsonl').open('w', encoding='utf-8') as states, \
                 (scratch / 'events.jsonl').open('w', encoding='utf-8') as events:
                def emit(event):
                    events.write(_dump(event) + '\n')
                    counts['candidate_events'] += 1
                for (group,) in connection.execute('SELECT DISTINCT grp FROM vectors ORDER BY grp'):
                    counts['groups'] += 1
                    reference = fit_reference(rows_for(group, reference=True), min_reference)
                    points = []
                    for sample in reference.pop('_sample_rows'):
                        normalized = normalize(sample, reference)
                        if normalized['coverage'] >= minimum_overlap:
                            points.append(normalized['values'])
                    clustering = fit_clusters(points, k=clusters, minimum_overlap=minimum_overlap)
                    counts['ready_models'] += reference['status'] == 'ready'
                    models.write(_dump({'group_id': group, 'reference': reference, 'clustering': clustering}) + '\n')
                    builder, previous = EventBuilder(), None
                    for row in rows_for(group):
                        if previous is not None and row['start_time'] < previous[0]['end_time']:
                            raise ValueError('overlapping windows within native series')
                        row['in_reference'] = reference_start is None or (row['start_time'] >= reference_start and row['end_time'] <= reference_end)
                        state = assess(row, reference, clustering, mode, thresholds, previous)
                        matrices.write(_dump(row) + '\n')
                        states.write(_dump(state) + '\n')
                        statuses[state['scoring_status']] += 1
                        source_counts[row['source']] += 1
                        counts['candidate_windows'] += state['candidate']
                        counts['state_changes'] += state['state_changed']
                        counts['quality_signal_windows'] += bool(state['quality_signals'])
                        counts['references_truncated_states'] += row['references_truncated']
                        for trigger in state['triggers']:
                            trigger_counts[':'.join(trigger.split(':')[:2])] += 1
                        for event in builder.push(row, state):
                            emit(event)
                        previous = row, state
                    for event in builder.finish():
                        emit(event)
                    if progress and counts['groups'] % 100 == 0:
                        progress({'event': 'groups', 'groups': counts['groups']})
        except sqlite3.IntegrityError as error:
            raise ValueError('duplicate window_id in input') from error
        finally:
            connection.close()
        summary = {'schema_version': 1, 'batch': batch, 'windows_dir': str(root), 'output_dir': str(output),
                   'input_summary_sha256': hashlib.sha256(summary_bytes).hexdigest(), 'input_windows_sha256': digest.hexdigest(),
                   'input_scope': {key: input_summary.get(key) for key in ('mode', 'complete', 'window_count', 'rows_scanned', 'max_rows_per_file', 'naive_timezone', 'timezone_officially_confirmed')},
                   'reference_scope': {'mode': 'explicit_interval' if reference_start is not None else 'offline_same_batch',
                                       'start': reference_start, 'end': reference_end, 'known_healthy': False},
                   'config': {**thresholds, 'mode': mode, 'clusters': clusters, 'min_reference': min_reference},
                   'capacities': {'features': 128, 'reference_samples': 256, 'cluster_samples': 64, 'categories': 16, 'event_refs': 16, 'event_windows': 64},
                   **{key: counts[key] for key in ('states', 'groups', 'ready_models', 'candidate_windows', 'candidate_events', 'state_changes', 'quality_signal_windows', 'references_truncated_states')},
                   'scoring_status_counts': dict(sorted(statuses.items())), 'trigger_counts': dict(sorted(trigger_counts.items())),
                   'states_by_source': dict(sorted(source_counts.items())), 'diagnoses_confirmed': False}
        (scratch / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
        (scratch / 'report.md').write_text(_report(summary), encoding='utf-8')
        _publish(scratch, output)
    return summary

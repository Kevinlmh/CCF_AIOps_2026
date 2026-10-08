"""Offline disk sort and exclusive publication of traceable feature artifacts."""

from collections import Counter
import csv
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from aiops_v4.data.discovery import SOURCE_PREFIXES, discover_sources
from aiops_v4.data.reader import RecordReader
from .aggregate import iter_windows, METRIC_LIMIT, REFERENCE_LIMIT, RESERVOIR_SIZE
from .semantics import metric_semantics, OFFICIAL_URL
from .series import prepare_record
from .time import time_zone


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _validate(root, batch, output, naive_timezone, width, step, gap, max_rows):
    if not isinstance(batch, str) or not batch.strip():
        raise ValueError('batch must be non-empty')
    for name,value in [('window_seconds',width),('counter_max_gap_seconds',gap),('max_rows_per_file',max_rows)]:
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(f'{name} must be positive')
    if step is not None and (type(step) is not int or step <= 0 or width % step):
        raise ValueError('expected_step_seconds must be a positive divisor of window_seconds')
    time_zone(naive_timezone)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'output directory already exists: {output}')
    root, output = root.resolve(), output.resolve()
    if root == output or root in output.parents:
        raise ValueError('output directory must be outside input root')


def _publish(artifacts, destination):
    # Claim a new destination atomically. summary.json is the completion marker.
    destination.mkdir(exist_ok=False)
    published = []
    try:
        for name in ['windows.jsonl','rejected.jsonl','semantics.json','semantics.csv','report.md','summary.json']:
            os.link(artifacts/name,destination/name)
            published.append(name)
    except OSError:
        # Remove only this invocation's links, never pre-existing/user-created files.
        for name in published:
            target=destination/name
            if target.is_file() and os.path.samestat(target.stat(),(artifacts/name).stat()):
                target.unlink()
        try:
            destination.rmdir()
        except OSError:
            pass
        raise


def _write_metadata(summary, catalog, artifacts):
    (artifacts/'semantics.json').write_text(json.dumps(catalog,ensure_ascii=False,allow_nan=False,indent=2)+'\n',encoding='utf-8')
    with (artifacts/'semantics.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=['source','metric','kind','unit','status','verified','basis'])
        writer.writeheader()
        writer.writerows(catalog.values())
    lines=['# v4 窗口特征报告','',f"批次：{summary['batch']}；模式：{summary['mode']}；完整扫描：{summary['complete']}。",
           f"读取 {summary['rows_scanned']} 条，进入窗口 {summary['rows_accepted']} 条，拒绝时间解析 {summary['rows_rejected']} 条；输出 {summary['window_count']} 个窗口。",'',
           f"无时区时间配置：{summary['naive_timezone']}；窗口 {summary['window_seconds']} 秒；预期采样步长 {summary['expected_step_seconds']}；计数器最大间隔 {summary['counter_max_gap_seconds']} 秒。",'',
           '这些参数是显式运行配置；无时区时间和字段单位仍保留核验状态。缺失率只覆盖已到达记录中的适用字段，时间覆盖率使用显式步长假设。',
           '缺少窗口不补零，第二批缺少详细业务流指标和 FRR 是官方数据安排。', '',
           f'来源：[官方数据与规则说明]({OFFICIAL_URL})。NetFlow 汇总为网元/接口/协议数量，不推断实际业务路径。','',
           '| 来源 | 文件 | 读取记录 | 窗口 |','|---|---:|---:|---:|']
    lines.extend(f"| {source} | {value['files']} | {value['rows']} | {value['windows']} |" for source,value in summary['sources'].items())
    lines.extend(['','## 质量与证据','',
                  '完整质量标记见 summary.json 和各窗口。references 保留原始位置及 counter 前驱；超过上限明确截断。',
                  '按 summary.json 的 input_root 加 source_file 找到原始 CSV，使用 record_index 和物理行号找回完整字段/日志。',
                  '分位数蓄水池容量 128，原始引用上限 8；按原始序列排序后处理，统计不使用标签或跨批次数据。',
                  'prefix_sample 的每文件前缀不能代表全批次，样本末端窗口可能不完整。',
                  '尚未实现参考尺度、聚类、候选事件或 LLM 诊断，本报告不包含故障判断。'])
    (artifacts/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (artifacts/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,allow_nan=False,indent=2)+'\n',encoding='utf-8')


def build_features(root: Path, batch: str, output_dir: Path, *, naive_timezone: str,
                   window_seconds: int = 60, expected_step_seconds: int | None = None,
                   counter_max_gap_seconds: int = 180, max_rows_per_file: int | None = None,
                   progress=None) -> dict:
    root, output_dir = Path(root), Path(output_dir)
    _validate(root,batch,output_dir,naive_timezone,window_seconds,expected_step_seconds,counter_max_gap_seconds,max_rows_per_file)
    files=discover_sources(root)
    if not files:
        raise ValueError('no recognized CSV files in input')
    output_dir.parent.mkdir(parents=True,exist_ok=True)
    sources={source:{'files':0,'rows':0,'windows':0} for source in SOURCE_PREFIXES}
    catalog, manifests = {}, []
    rows_scanned=rows_accepted=rows_rejected=window_count=0
    flags, views = Counter(),Counter()
    identity_samples={}
    identity_overflow=0
    references_truncated=0
    with tempfile.TemporaryDirectory(prefix='.v4-window-',dir=output_dir.parent) as folder:
        scratch=Path(folder)
        artifacts=scratch/'artifacts'
        artifacts.mkdir()
        connection=sqlite3.connect(scratch/'sort.sqlite')
        try:
            connection.execute('PRAGMA temp_store=FILE')
            connection.execute('PRAGMA cache_size=-8192')
            connection.execute('CREATE TABLE observations (series TEXT, timestamp TEXT, payload TEXT)')
            pending=[]

            def flush():
                connection.executemany('INSERT INTO observations VALUES (?,?,?)',pending)
                pending.clear()

            with (artifacts/'rejected.jsonl').open('w',encoding='utf-8') as rejected:
                for file in files:
                    if progress: progress({'event':'file_start','path':file.relative_path})
                    sources[file.source]['files']+=1
                    with RecordReader(file,batch,max_rows_per_file) as reader:
                        for record in reader:
                            rows_scanned+=1
                            sources[file.source]['rows']+=1
                            try:
                                prepared=prepare_record(record,naive_timezone)
                            except ValueError as error:
                                rows_rejected+=1
                                rejected.write(_dump({'reason':str(error),'record':record.to_dict()})+'\n')
                                continue
                            rows_accepted+=1
                            for name in prepared['raw_values']:
                                key=f'{file.source}.{name}'
                                if key not in catalog:
                                    if len(catalog)>=METRIC_LIMIT:
                                        raise ValueError('semantic catalog limit exceeded')
                                    catalog[key]=metric_semantics(file.source,name)
                            pending.append((prepared['series_key'],prepared['timestamp'],_dump(prepared)))
                            if len(pending)>=1000: flush()
                    manifests.append({'path':file.relative_path,'source':file.source,'columns':reader.columns,
                                      'rows_scanned':reader.rows_scanned,'complete':reader.complete,
                                      'schema_issues':reader.schema_issues,'parsed_records_sha256':reader.digest.hexdigest()})
                    if progress: progress({'event':'file_done','path':file.relative_path,'rows':reader.rows_scanned})
            flush()
            connection.commit()
            if progress: progress({'event':'aggregation_start','rows':rows_accepted})
            cursor=connection.execute('SELECT payload FROM observations ORDER BY series,timestamp,rowid')
            ordered=(json.loads(row[0]) for row in cursor)
            with (artifacts/'windows.jsonl').open('w',encoding='utf-8') as output:
                for window in iter_windows(ordered,batch,window_seconds,expected_step_seconds,counter_max_gap_seconds):
                    output.write(_dump(window)+'\n')
                    window_count+=1
                    sources[window['source']]['windows']+=1
                    views[window['view']]+=1
                    flags.update(window['quality_flags'])
                    references_truncated+=window['references_truncated']
                    identity=window['identity']
                    if identity['entity_id'] not in identity_samples:
                        if len(identity_samples)<128:
                            identity_samples[identity['entity_id']]=identity
                        else:
                            identity_overflow+=1
        finally:
            connection.close()
        summary={'schema_version':1,'batch':batch,'input_root':str(root.resolve()),'output_dir':str(output_dir.resolve()),
                 'mode':'full' if max_rows_per_file is None else 'prefix_sample','max_rows_per_file':max_rows_per_file,
                 'complete':all(file['complete'] for file in manifests),'rows_scanned':rows_scanned,
                 'rows_accepted':rows_accepted,'rows_rejected':rows_rejected,'window_count':window_count,
                 'sources':sources,'views':dict(views),'quality_flags_by_window':dict(flags),
                 'files':manifests,'identity_samples':identity_samples,'identity_sample_limit':128,
                 'identity_overflow_windows':identity_overflow,'references_truncated_windows':references_truncated,
                 'window_seconds':window_seconds,'expected_step_seconds':expected_step_seconds,
                 'counter_max_gap_seconds':counter_max_gap_seconds,'naive_timezone':naive_timezone,
                 'timezone_officially_confirmed':False,'semantic_registry_limit':METRIC_LIMIT,
                 'quantile_capacity':RESERVOIR_SIZE,'reference_limit':REFERENCE_LIMIT,
                 'time_evidence':['Official SDK interprets naive observations as UTC.',
                                  'Public traffic Unix last-batch timestamps corroborate UTC; not an official guarantee for all sources.'],
                 'official_data_notes_url':OFFICIAL_URL}
        _write_metadata(summary,dict(sorted(catalog.items())),artifacts)
        _publish(artifacts,output_dir)
    return summary

"""Command-line profiling, optional evidence snapshots and read-only queries."""

import argparse
import csv
import json
from pathlib import Path
import sqlite3
import sys

from .discovery import SOURCE_PREFIXES
from .profile import profile_dataset
from .store import ingest_dataset, query_records


def _positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _parser():
    parser = argparse.ArgumentParser(description="v4 raw-data foundation (no diagnosis inference)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("profile", "ingest"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--batch", required=True)
        command.add_argument("--report-dir", type=Path, required=True)
        command.add_argument("--max-rows-per-file", type=_positive)
        command.add_argument("--verbose", action="store_true")
        if name == "profile":
            command.add_argument("--reservoir-size", type=_positive, default=512)
        else:
            command.add_argument("--database", type=Path, required=True)
    query = commands.add_parser("query")
    query.add_argument("--database", type=Path, required=True)
    query.add_argument("--batch", required=True)
    query.add_argument("--source", choices=SOURCE_PREFIXES)
    query.add_argument("--node-id")
    query.add_argument("--start", help="inclusive ISO timestamp with timezone")
    query.add_argument("--end", help="exclusive ISO timestamp with timezone")
    query.add_argument("--record-id")
    query.add_argument("--limit", type=_positive, default=100)
    return parser


def _outside_input(path, root):
    path = path.resolve()
    root = root.resolve()
    if path == root or root in path.parents:
        raise ValueError(f"output must be outside input root: {path}")


def _preflight(arguments):
    report_dir = arguments.report_dir
    if report_dir.exists() or report_dir.is_symlink():
        raise FileExistsError(f"report directory already exists: {report_dir}")
    _outside_input(report_dir, arguments.root)
    if arguments.command == "ingest":
        database = arguments.database
        if database.exists() or database.is_symlink():
            raise FileExistsError(f"database already exists: {database}")
        _outside_input(database, arguments.root)
        if database.resolve() == report_dir.resolve() or report_dir.resolve() in database.resolve().parents:
            raise ValueError("database must be outside the report directory")


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _publish_report(report, directory):
    directory.mkdir(parents=True, exist_ok=False)
    with (directory / "profile.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
    lines = ["# v4 原始数据统计报告", "",
             f"- 批次：{_cell(report['batch'])}",
             f"- 输入：{_cell(report['input_root'])}",
             f"- 模式：{report['mode']}；每文件行数上限：{report['max_rows_per_file']}",
             f"- 扫描完成：{report['complete']}；文件：{report['files_scanned']}；记录：{report['rows_scanned']}",
             f"- 解析后的时间范围：{report['start_time']} → {report['end_time']}",
             f"- 已识别网元：{len(report['entities'])}", "",
             "prefix_sample 是每文件前 N 行，不能代表整个批次的分布。full 的计数覆盖已发现文件；分位数仍可能是估计值。",
             "本报告不判定故障，也不推断故障数量、根因或类别。", "",
             "## 来源覆盖", "", "| 来源 | 文件 | 记录 |", "|---|---:|---:|"]
    lines.extend(f"| {source} | {counts['files']} | {counts['rows']} |" for source, counts in report["sources"].items())
    lines.extend(["", "## 质量标记", "", "| 标记 | 记录数 |", "|---|---:|"])
    lines.extend(f"| {_cell(flag)} | {count} |" for flag, count in sorted(report["quality_flags"].items()))
    lines.extend(["", f"有限窗口内重复记录：{report['recent_duplicate_rows']}；观测到的时间倒序：{report['out_of_order_rows']}。",
                  f"时序跟踪淘汰：{report['time_series_evictions']}；字段/指标/角色指标丢弃观测："
                  f"{report['dropped_field_observations']}/{report['dropped_metric_observations']}/{report['dropped_role_metric_observations']}。",
                  "", "## 字段与语义", "",
                  "fields.csv 提供字段类型、缺失/坏值计数及数值统计；原始值保留在观测中。",
                  "单位和计数器语义尚未验证，numeric_kind_hint 仅为字段名线索；没有差分、聚合或自动单位换算。",
                  "无时区时间暂按 UTC 转换并标记 assumed_utc，校准前不得直接用于事件时间评分。", "",
                  "## 文件结构与扫描错误", ""])
    for file in report["files"]:
        if file["schema_issues"]:
            lines.append(f"- {_cell(file['path'])}: {_cell(', '.join(file['schema_issues']))}")
    for error in report["errors"]:
        lines.append(f"- {_cell(error['path'])}: {_cell(error['error'])}")
    lines.extend(["", "## 统计边界", ""])
    lines.extend(f"- {note}" for note in report["notes"])
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    columns = ["field", "kind", "unit", "semantic_verified", "numeric_kind_hint", "count", "missing_count",
               "numeric_count", "non_numeric_count", "nonfinite_count", "zero_count", "min", "max", "mean", "std",
               "median", "mad", "p01", "p05", "q25", "q75", "p95", "p99",
               "distinct_count_lower_bound", "distinct_exact", "quantile_sample_count", "quantiles_exact"]
    with (directory / "fields.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for name, statistics in sorted(report["fields"].items()):
            writer.writerow({"field": name, **{key: statistics.get(key) for key in columns[1:]}})


def main(argv=None):
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "query":
            rows = query_records(arguments.database, arguments.batch, source=arguments.source,
                                 node_id=arguments.node_id, start=arguments.start, end=arguments.end,
                                 record_id=arguments.record_id, limit=arguments.limit)
            for row in rows:
                print(json.dumps(row, ensure_ascii=False, allow_nan=False))
            return 0
        _preflight(arguments)

        def progress(event):
            if arguments.verbose:
                print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)

        if arguments.command == "profile":
            report = profile_dataset(arguments.root, arguments.batch, arguments.max_rows_per_file,
                                     arguments.reservoir_size, progress=progress)
        else:
            report = ingest_dataset(arguments.root, arguments.batch, arguments.database,
                                    arguments.max_rows_per_file, progress=progress)
        _publish_report(report, arguments.report_dir)
        summary = {key: report[key] for key in ("batch", "mode", "complete", "files_scanned", "rows_scanned")}
        summary["report_dir"] = str(arguments.report_dir.resolve())
        print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
        return 2 if report["errors"] else 0
    except (OSError, ValueError, sqlite3.DatabaseError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

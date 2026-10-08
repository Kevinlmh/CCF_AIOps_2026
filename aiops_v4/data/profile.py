"""Batch-local CSV profiling, preserving scope and quality of every scan."""

from collections import Counter, OrderedDict
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path

from .discovery import SOURCE_PREFIXES, discover_sources, numeric_columns
from .reader import RecordReader
from .statistics import ColumnStats


def profile_dataset(root: Path, batch: str, max_rows_per_file: int | None = None,
                    reservoir_size: int = 512, *, max_groups: int = 2048,
                    cardinality_limit: int = 128, on_record=None, progress=None) -> dict:
    """Scan one batch. Quantiles are estimated; counters cover scanned rows only."""
    for name, limit in [("max_rows_per_file", max_rows_per_file), ("reservoir_size", reservoir_size),
                        ("max_groups", max_groups), ("cardinality_limit", cardinality_limit)]:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError(f"{name} must be positive")
    if not isinstance(batch, str) or not batch.strip():
        raise ValueError("batch must be non-empty")
    files = discover_sources(Path(root))
    if not files:
        raise ValueError(f"no recognized CSV files in {root}")
    fields, metrics, role_metrics = {}, {}, {}
    field_kinds = {}
    quality = Counter()
    entities = Counter()
    sources = {source: {"files": 0, "rows": 0} for source in SOURCE_PREFIXES}
    manifests, errors = [], []
    duplicate_count = out_of_order = series_evictions = entity_overflow = 0
    dropped = {"field": 0, "metric": 0, "role_metric": 0}
    recent = OrderedDict()
    last_times = OrderedDict()
    interval_stats = ColumnStats(True, reservoir_size, cardinality_limit)
    start = end = None
    row_count = 0

    def update(mapping, key, raw, numeric=True, group_limit=max_groups, kind="metric"):
        if key not in mapping:
            if len(mapping) >= group_limit:
                dropped[kind] += 1
                return
            mapping[key] = ColumnStats(numeric, reservoir_size, cardinality_limit)
        mapping[key].add(raw)

    for file in files:
        stat = file.path.stat()
        entry = {"path": file.relative_path, "source": file.source, "size_bytes": stat.st_size,
                 "mtime_ns": stat.st_mtime_ns, "rows_scanned": 0, "complete": False,
                 "start_time": None, "end_time": None, "columns": [], "schema_issues": []}
        sources[file.source]["files"] += 1
        reader = RecordReader(file, batch, max_rows_per_file)
        if progress:
            progress({"event": "file_start", "path": file.relative_path, "source": file.source})
        try:
            with reader:
                numeric = set(numeric_columns(file.source, reader.columns))
                for record in reader:
                    row_count += 1
                    sources[file.source]["rows"] += 1
                    quality.update(record.quality_flags)
                    for name, raw in record.raw.items():
                        key = f"{file.source}.{name}"
                        kind = "metric" if name in numeric else "text" if name in {"message", "scrape_error"} else "identity"
                        field_kinds.setdefault(key, kind)
                        update(fields, key, raw, name in numeric, group_limit=4096, kind="field")
                    for name, value in record.values.items():
                        update(metrics, f"{file.source}.{name}", str(value))
                        if record.node_id:
                            role = record.node_id.split("-", 1)[1]
                            update(role_metrics, f"{file.source}/{role}/{name}", str(value), kind="role_metric")
                    if record.node_id:
                        if record.node_id in entities or len(entities) < 80:
                            entities[record.node_id] += 1
                        else:
                            entity_overflow += 1
                    if record.timestamp:
                        timestamp = record.timestamp
                        start = timestamp if start is None else min(start, timestamp)
                        end = timestamp if end is None else max(end, timestamp)
                        entry["start_time"] = timestamp if entry["start_time"] is None else min(entry["start_time"], timestamp)
                        entry["end_time"] = timestamp if entry["end_time"] is None else max(entry["end_time"], timestamp)
                        series = (file.relative_path, record.node_id, tuple(sorted(record.dimensions.items())))
                        old = last_times.pop(series, None)
                        if old is not None:
                            if timestamp < old:
                                out_of_order += 1
                            elif timestamp > old:
                                delta = (datetime.fromisoformat(timestamp.replace("Z", "+00:00")) -
                                         datetime.fromisoformat(old.replace("Z", "+00:00"))).total_seconds()
                                interval_stats.add(str(delta))
                        last_times[series] = timestamp
                        if len(last_times) > 4096:
                            last_times.popitem(last=False)
                            series_evictions += 1
                    content = json.dumps([record.raw, record.extra_cells], ensure_ascii=False, separators=(",", ":"))
                    key = (file.relative_path, hashlib.sha256(content.encode()).digest())
                    if key in recent:
                        duplicate_count += 1
                        recent.move_to_end(key)
                    else:
                        recent[key] = None
                        if len(recent) > 4096:
                            recent.popitem(last=False)
                    if on_record:
                        on_record(record)
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            errors.append({"path": file.relative_path, "error": str(exc)})
        entry.update(columns=reader.columns, schema_issues=reader.schema_issues,
                     rows_scanned=reader.rows_scanned, complete=reader.complete,
                     parsed_records_sha256=reader.digest.hexdigest())
        manifests.append(entry)
        if progress:
            progress({"event": "file_done", "path": file.relative_path, "rows": reader.rows_scanned,
                      "complete": reader.complete})

    field_reports = {}
    for name, statistics in fields.items():
        field = name.split(".", 1)[1]
        hint = "counter" if field.endswith(("_total", "_sum", "_count")) else None
        field_reports[name] = {**statistics.report(), "kind": field_kinds[name], "unit": None,
                               "numeric_kind_hint": hint, "semantic_verified": False}
    return {
        "schema_version": 1, "batch": batch, "input_root": str(Path(root).resolve()),
        "mode": "full" if max_rows_per_file is None else "prefix_sample",
        "max_rows_per_file": max_rows_per_file,
        "complete": not errors and all(file["complete"] for file in manifests),
        "files_scanned": len(manifests), "rows_scanned": row_count,
        "start_time": start, "end_time": end, "sources": sources,
        "fields": field_reports, "metrics": {key: value.report() for key, value in metrics.items()},
        "role_metrics": {key: value.report() for key, value in role_metrics.items()},
        "entities": dict(sorted(entities.items())), "quality_flags": dict(quality),
        "recent_duplicate_rows": duplicate_count, "duplicate_check_window": 4096,
        "out_of_order_rows": out_of_order, "time_series_tracking_limit": 4096,
        "time_series_evictions": series_evictions, "interval_seconds": interval_stats.report(),
        "dropped_field_observations": dropped["field"], "dropped_metric_observations": dropped["metric"],
        "dropped_role_metric_observations": dropped["role_metric"], "entity_overflow_rows": entity_overflow,
        "files": manifests, "errors": errors,
        "notes": ["Quantiles and MAD use a bounded deterministic reservoir, not every numeric value.",
                  "Prefix samples are the first N rows of each file, not representative random samples.",
                  "Recent duplicate and interval checks are bounded; no rows are removed.",
                  "Naive timestamps are assumed UTC and flagged; timezone semantics need verification.",
                  "Parsed-record digests are not hashes of original file bytes.",
                  "Intervals describe observed recurrence and do not establish an expected sampling cadence."],
    }

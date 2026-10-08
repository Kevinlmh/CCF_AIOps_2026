# v4 Data Foundation Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. User explicitly requested starting implementation in this conversation; proceed without a second authorization gate.

**Goal:** Deliver reproducible raw-data profiling, lossless normalized records and read-only evidence queries for v4.

**Architecture:** Stream CSV into lossless records. Independently calculate bounded statistics or persist the same records to SQLite; CLI exposes both and batch-scoped querying.

**Tech Stack:** Python >=3.10, standard library csv/json/sqlite3/statistics/hashlib, pytest.

**Spec:** docs/superpowers/specs/2026-10-08-v4-design.md

## Global Constraints

- Python >=3.10, standard library runtime dependencies.
- No deletions or overwrites of existing data, outputs or submit.py.
- Batch required, no cross-batch statistics/learning.
- No feature aggregation, counter differencing, LLM or cluster inference in this milestone.
- Bound memory, report sampling and incomplete/error status explicitly.
- Implement in the user-selected v4 checkout; no concurrent implementers.

## Review Focus

- Malformed headers/rows must not silently produce clean evidence.
- Unknown entities and timestamps remain visible without fabricated mappings.
- Fractional timestamps and timezone offsets must filter correctly.
- Existing output files and databases must survive failed or repeated commands.
- High-cardinality NetFlow/labels must not make memory unbounded or imply complete statistics.

## Task 1: Source discovery and lossless normalized records

Files: aiops_v4/__init__.py, aiops_v4/data/{__init__,contracts,discovery,reader}.py; tests/v4/test_records.py.

Interfaces: discover_sources(root: Path) -> list[SourceFile]; RecordReader(file: SourceFile, batch: str, max_rows: int|None) context manager yielding RawRecord; RawRecord.to_dict() -> dict.

- [x] Write tests for seven source families, zero/missing/nonfinite values, metric labels, full logs, raw flow addresses, valid role boundaries, stable IDs, timezones, multiline CSV and empty/ambiguous headers.
- [x] Run tests: expected assertion failure because aiops_v4 is absent.
- [x] Implement discovery and reader with provenance and field-level quality flags.
- [x] Run tests: expected all records tests and existing shared tests pass.
- [x] Commit reader deliverable.

## Task 2: Bounded dataset profiling

Files: aiops_v4/data/{statistics,profile}.py; tests/v4/test_profile.py.

Interfaces: profile_dataset(root: Path, batch: str, max_rows_per_file: int|None = None, reservoir_size: int = 512, on_record=None, progress=None) -> dict. Consumes Task 1 records, invokes optional record sink used in Task 3.

- [x] Write tests asserting literal counts, means/quantiles, separate routing metrics, explicit prefix sampling, quality warnings, bounded cardinality and rejection of empty discovery.
- [x] Run tests: expected failure because profile_dataset is absent.
- [x] Implement streaming statistics and field/metric/entity/time coverage reports.
- [x] Run tests: expected profile tests and complete suite pass.
- [x] Commit profiling deliverable.

## Task 3: Transactional evidence storage and batch-scoped queries

Files: aiops_v4/data/store.py; tests/v4/test_store.py.

Interfaces: ingest_dataset(root: Path, batch: str, database: Path, max_rows_per_file: int|None = None, progress=None) -> dict; query_records(database: Path, batch: str, *, source=None, node_id=None, start=None, end=None, record_id=None, limit=100) -> list[dict]. Consumes Task 2 on_record sink and report metadata.

- [x] Write integration tests for full evidence recovery, time ranges/offsets, invalid timestamp rows, batch isolation, missing/read-only databases, bounded queries and existing-file protection.
- [x] Run tests: expected failure because store module is absent.
- [x] Implement new database publication using temporary SQLite file, bounded transactions and read-only URI queries.
- [x] Run tests: expected store tests and full suite pass.
- [x] Commit storage deliverable.

## Task 4: CLI, real-data verification and documentation

Files: aiops_v4/data/{cli,__main__}.py; tests/v4/test_cli.py; pyproject.toml; README.md; docs/v4-data-foundation-2026-10-08.md.

Interfaces: python -m aiops_v4.data profile/ingest/query. Input root/batch required for profile/ingest; database/batch required for query. --report-dir publishes profile.json plus human-readable report.md and fields.csv; refuses existing report artifacts.

- [x] Write subprocess tests for commands, query content, repeated-output refusal and invalid arguments.
- [x] Run tests: expected CLI module missing.
- [x] Implement commands and include aiops_v4 in package discovery.
- [x] Run full tests, package install and real sample/stage2 checks; verify existing protected files.
- [x] Review final diff; fix material findings with failing regression tests first.
- [x] Save measured results, remaining milestones and next task; commit.

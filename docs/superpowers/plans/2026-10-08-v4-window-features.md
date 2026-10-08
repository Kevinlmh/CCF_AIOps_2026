# v4 Window Features Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans, sequential implementation followed by one fresh read-only reviewer. User explicitly requested continuing the accepted milestone; no repeated approval gate.

**Goal:** Deliver documented metric semantics and traceable multiview windows for clustering.
**Architecture:** Existing raw reader → explicit time/entity/metric policies → temporary SQLite sort → streaming windows → new JSONL/report artifacts.
**Tech Stack:** Python >=3.10, standard library sqlite3/zoneinfo/csv/json, pytest.
**Spec:** docs/superpowers/specs/2026-10-08-v4-window-features-design.md

## Global Constraints

- Preserve existing data, outputs and submit.py; current v4 checkout, no concurrent implementers.
- Standard library runtime, explicit batch/timezone, no labels/LLM/clustering this milestone.
- No invented empty windows or zeros; retain identity, evidence and quality limitations.
- Offline disk sorting; memory bounded by one series/window, 128 quantile samples and 8 references.
- Prefix sampling never implies full-batch statistics; output must be new and outside input.

## Review Focus

- Equal-time conflicts, missing/bad counter samples and long gaps must break differencing.
- Cross-file timestamps and legitimate many-record NetFlow quantities must be processed correctly.
- Unknown/auxiliary entities and missing dimensions must not collapse into real candidates.
- Original naive timestamps must honor explicit timezone, including ambiguous/nonexistent DST.
- Failure, repeated output and high-cardinality flows must not alter existing files or consume unbounded memory.

## Task 1: Semantic registry and identity/time policies

Files: aiops_v4/features/{__init__,semantics,identity,time}.py, tests/v4/test_semantics.py.
Interfaces: metric_semantics(source,metric)->dict; entity_identity(record)->dict; observation_time(record,naive_timezone)->(UTC datetime, flags); window_start(datetime,width)->datetime.

- [ ] Write tests: percent vs ratio, direct rates, inferred counters vs unknown, NetFlow interval counts, UTC/+08, original naive reinterpretation, boundary, auxiliary identity, DST rejection.
- [ ] Run tests, expected module/function missing failures.
- [ ] Implement registry with evidence/status and explicit policies.
- [ ] Run tests + full suite, expected green; commit.

## Task 2: Sorted series and window statistics

Files: aiops_v4/features/{series,aggregate}.py, tests/v4/test_windows.py.
Interfaces: series_dimensions(record)->dict; iter_windows(ordered_records,batch,window_seconds,expected_step_seconds,counter_max_gap_seconds)->iterator[dict]. ordered_records contains normalized dicts with original refs, values/raw/flags and semantic/identity metadata.

- [ ] Tests: gauge literal mean/min/max/std/slope, missing counts, no fake windows, counter rates/reset/gap/missing/conflict, routing peer/label isolation, NetFlow additive counts, bounded references/quantile metadata.
- [ ] Run tests, expected missing implementation failures.
- [ ] Implement grouped sorted processing; validation and units metadata.
- [ ] Run tests + full suite, expected green; commit.

## Task 3: CSV build pipeline and CLI

Files: aiops_v4/features/{pipeline,cli,__main__}.py, tests/v4/test_features_cli.py.
Interfaces: build_features(root,batch,output_dir,*,naive_timezone,window_seconds=60,expected_step_seconds=None,counter_max_gap_seconds=180,max_rows_per_file=None)->dict; python -m aiops_v4.features build ... .

- [ ] Subprocess/integration tests for all-source fixtures, cross-file out-of-order counters, rejected bad time, batch metadata, prefix scope, protected output and malformed CSV failure.
- [ ] Run tests, expected module/CLI missing failures.
- [ ] Implement bounded insert batches and SQLite disk sort, exclusive artifacts and semantic exports.
- [ ] Run full suite/install/CLI outside cwd, expected green; commit.

## Task 4: Real-data verification, final review and delivery

Files: README.md, docs/v4-window-features-2026-10-08.md, docs/v4-window-features-progress.md.

- [ ] Run full public case and stage2 prefix, verify feature/quality counts and protected files.
- [ ] Dispatch one read-only whole-change reviewer; material findings require regression RED→GREEN.
- [ ] Record evidence, limitations, next clustering task; all checks green; commit and keep v4 branch.

# v4 implementation progress

Plan: docs/superpowers/plans/2026-10-08-v4-data-foundation.md

User authorized implementation of the chosen architecture and phased delivery. First milestone: raw-data foundation.

Pre-flight: Task 1 RawRecord → Task 2 statistics/sink → Task 3 persistence → Task 4 CLI; interfaces consistent.

Execution: current user-selected v4 checkout, sequential implementation. No model, clustering or hidden labels are used in this milestone.

Task 1: complete — 17 new reader tests + 9 shared tests pass (26 total), including full multiline logs and seven sources.

Task 2: complete — bounded profiles, role/metric separation, explicit prefix sampling, errors and quality counts; full suite 37 passed.

Task 3: complete — atomic non-overwriting SQLite snapshots, persisted scan scope and batch-scoped read-only evidence queries; full suite 47 passed.

Task 4: CLI and packaging implemented — 57 tests pass, editable install and CLI invocation outside checkout pass. Real-data scans and whole-branch review in progress.

Task 4 regression checks: large valid CSV log field (256 KiB) and exported field count columns reproduced failures, then passed after removing the default CSV cutoff and aligning CSV dictionary columns with statistics names.

Final review: one fresh-context, read-only reviewer (gpt-6-astra). Three Important findings and one Minor overflow finding; overflow regraded Important because a nonfinite accumulator must not imply a valid zero variance. No findings deferred.

Final: fixed leading blank CSV lines — test_leading_blank_lines_do_not_hide_valid_csv_data RED→GREEN, provenance preserved.

Final: fixed mixed routing.value distribution — routing separation and CLI dictionary tests RED→GREEN; generic column retains structural counts, numerical distributions only by metric_name.

Final: fixed unknown explicit region fallback — test_unknown_explicit_traffic_region_is_not_mapped_using_directory_city RED→GREEN, invalid source remains unmapped.

Final: fixed overflow masking std — test_finite_extreme_values_do_not_report_zero_std_after_overflow RED→GREEN, unavailable statistic explicitly flagged.

Final regression suite: 61 passed. Real-data reports are regenerated under new *-reviewed paths, preserving earlier runs.

Final: Ruling: reviewer did not judge real-data results — author verifies actual runs and measured scope below — cost if wrong: incorrect dataset coverage claims.

Final: Ruling: future feature/clustering/LLM behavior remains outside milestone 1 — matches phased spec, delivered as explicit remaining work — cost if wrong: additional architecture/implementation changes next milestone.

Final: Ruling: submit.py is not read or executed — protected file metadata is checked instead — cost if wrong: submission integration still requires later verification.

Task 4: complete — final code scans sample/case_001: 56 files, 604,284 rows, no scan errors; stage2 prefix: 120 files, 120,000 rows, explicit incomplete scope, no scan errors; SQLite prefix: 4,477 rows and batch-scoped evidence query verified. Existing 344 protected files unchanged by size/mtime; v3 archive tag unchanged. Full suite 61 passed and git diff --check clean.

Delivery: keep the user-selected v4 branch and checkout in place; no integration into v3/baseline and no remote publish was requested. Next milestone and remaining work are recorded in docs/v4-data-foundation-2026-10-08.md.

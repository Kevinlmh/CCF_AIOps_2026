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

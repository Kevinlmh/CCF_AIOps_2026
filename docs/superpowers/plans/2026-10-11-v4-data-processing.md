# v4 data processing implementation plan

> **For agentic workers:** Use superpowers:executing-plans, task by task, with tests before implementation and one final independent review.

**Goal:** Build a reusable data backend which parses CSV once, stores minute observations once, and serves existing state/evidence tools without changing detection or LLM behavior.

**Architecture:** A byte-position reader feeds resumable private spools. Immutable normalized feature, state and evidence SQLite stores are linked through checked relative dependencies. Existing JSONL and sealed experiment readers remain supported.

**Tech Stack:** Python 3.10+, standard library, SQLite, multiprocessing, pytest for verification.

**Spec:** `docs/superpowers/specs/2026-10-11-v4-data-processing-redesign.md` (approved by the user's instruction to start implementation).

## Global Constraints

- Preserve candidate thresholds, same-batch fitting, role inputs and prompts; no model calls this round.
- Preserve IDs, ordering, masks, integer counts and references; float tolerance `rel_tol=1e-10, abs_tol=1e-12`.
- Float64 statistics with explicit missing values; three separately sealed databases; no duplicated full matrix/state import into evidence.
- Default 4 workers (1–8), 4 GiB memory budget, 12 GiB reserve, 100 GiB workspace ceiling. Raw files read only.
- Keep stopped server jobs stopped. No GitHub push, contest submission or full-batch job.
- Work in the user-requested local v4 checkout. Freeze its dirty working sources before changing them; preserve unrelated deletions and report.

## Review Focus

1. CSV byte boundaries: BOM, CR/LF, blank physical lines and quoted multiline fields must preserve original logical indices and IDs (Task 1).
2. Cross-file counters, timestamp ties and negative/nonfinite values must produce identical windows and candidates (Tasks 2–3).
3. Interruption after a committed chunk must not duplicate rows or publish partial results; input mutations must invalidate resume (Tasks 1–2, 5).
4. Relocated stores, path traversal and changed upstream bytes must fail safely or resolve within the sealed root (Tasks 3–4).
5. Query paging, role preparation and raw evidence must agree with legacy results, including suppressed states and auxiliary identities (Task 4).

## Tasks

### Task 1: Byte-addressed parsing and resumable input identity

**Files:** Add `aiops_v4/data/position.py`, `tests/v4/test_position_reader.py`.
**Interfaces:** `PositionReader(file, batch, max_rows=None, checkpoint=None)` yields positions and CSV cells; `record_at(root, batch, manifest, anchor)` reconstructs and verifies existing `RawRecord` identity from an exact byte range.

- [x] Write fixtures for CSV boundary cases, exact range retrieval, mutation/path rejection, and continuation after a saved boundary; watch tests fail.
- [x] Implement binary physical-line iterator and strict logical CSV reader, per-file header/identity/hash metadata, and direct reference lookup.
- [x] Run reader tests and existing record/raw tests; document results in the ledger.

### Task 2: Canonical features and one-pass parallel builder

**Files:** Add `aiops_v4/features/store.py`, `aiops_v4/features/build.py`, `aiops_v4/features/spool.py`, `aiops_v4/data/essential.py`, `tests/v4/test_feature_store.py`.
**Interfaces:** `build_feature_store(root,batch,output_dir,**policy)` returns feature summary; `FeatureStore(path)` exposes ordered windows, group queries and registered byte anchors; `export_windows(path,destination)` is optional.

- [x] Write failing round-trip, legacy equivalence, cross-file counter, NetFlow, worker determinism, one-parse, budget and resume tests.
- [x] Implement dictionaries, fixed-layout typed statistics with missing bitmap, file spools and essential profile collected during parsing. Preserve existing reducers; merge file contributions in deterministic order.
- [x] Verify exact references/counts/masks and float tolerances against legacy fixtures; run full suite.

### Task 3: State deltas over canonical windows

**Files:** Add `aiops_v4/states/store.py`, `aiops_v4/states/build.py`, `tests/v4/test_state_store.py`; adapt `aiops_v4/states/pipeline.py` dispatch.
**Interfaces:** `build_state_store(features_dir,batch,output_dir,**settings)` computes unchanged per-group models/signals/admission/events; `StateStore` reconstructs matrices in batches from the feature dependency.

- [x] Write failing comparison of models, states, events and ordered queries against existing state discovery, including interruption and relocation.
- [x] Implement group progress and state-only persistence; derive vectors on reads and record new provenance hash names.
- [x] Verify candidates unchanged for synthetic and public inputs; run full suite.

### Task 4: Evidence references and existing tool compatibility

**Files:** Add `aiops_v4/evidence/linked.py`, `tests/v4/test_linked_evidence.py`; adapt evidence pipeline/index and role preparation access where required.
**Interfaces:** Existing `read_index`, `query_evidence`, `query_raw` support linked stores; state/matrix/model SQL compatibility uses views/functions against upstream stores, never materialized full copies.

- [x] Write failing comparison of bundles, relation/membership queries, all state pages and role preparations. Include changed source and unsafe dependency paths.
- [x] Build small evidence database with references and registered snapshots; resolve finite raw references by byte range.
- [x] Verify tool results and role inputs; run full suite with no model calls.

### Task 5: Layer reuse, execution policy and CLI

**Files:** Add `aiops_v4/data/cache.py`, `aiops_v4/data/resources.py`, `aiops_v4/data/__main__.py`, `tests/v4/test_data_cache.py`; adapt experiment config/run dispatch and document commands.
**Interfaces:** Layer signatures cover real producer dependencies/config/input, independent of model settings; `python -m aiops_v4.data build/verify/export` manages the new backend explicitly.

- [x] Write failing invalidation/resume/seal tests: model changes reuse data; parsing changes invalidate features; state changes invalidate descendants; workspace/reserve failure retains checkpoints.
- [x] Implement atomic stage seals, checked dependencies and backend selection, keeping legacy default until performance gates pass.
- [x] Verify completed runs and interrupted builds; run full suite.

### Task 6: Correctness and performance evidence

**Files:** Add `scripts/v4_data_benchmark.py`, `docs/v4-data-processing-results.md` and regression tests for measured behavior.

- [x] Compare the frozen baseline and new backend on three official samples and at least one million NetFlow records with nonflow/cross-file controls; no LLM (9 official comparisons + 1 million-prefix comparison all equivalent).
- [x] Alternate old/new three times, report median wall/CPU/RSS and final/peak disk with source fingerprints and candidate comparisons (Mac measurements; kernel I/O and server/full batch remain unmeasured).
- [x] Obtain one independent review; fix critical/important findings with failing regression tests, then run the full suite (7 important fixes, latest suite 468 passed).
- [x] Deliver code and measured limits; retain the explicit new backend if any acceptance gate has not passed (disk 23.8%, wall 43.1% of baseline; legacy stays default).

## Execution record

Task outcomes and rulings are recorded in `.superpowers/sdd/2026-10-11-v4-data-processing/progress.md`. Existing dirty files will not be bundled into a new commit implicitly. Snapshot: `outputs/v4/data_redesign_baseline/working_sources.tar.gz`.

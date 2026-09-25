# v2.0 Evidence-Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make existing FRR/NetFlow observations contribute bounded, traceable RCA evidence and audit every direct-detector event without using hidden labels or event-count targets.

**Architecture:** `source_support.py` converts masked log/NetFlow cells into `[T,N]` and `[T,E]` auxiliary scores. The direct path passes these scores to the existing ranking timeline while preserving source roles. A separate read-only audit command joins feature-store values with the prediction and inference JSON files, writing compact evidence records and summary.

**Tech Stack:** Python 3.11+, NumPy, PyTorch, pytest, existing `FeatureStore` and v2 CLI.

**Spec:** `docs/superpowers/specs/2026-09-24-v2-evidence-chain-design.md`

## Global Constraints

- Preserve `predictions.jsonl` schema, 1–30 minute global non-overlapping intervals and existing seven-source feature-store format.
- No LLM calls, no model training, no hidden labels, no event-count target, no overwrite of the 2026-09-24 eight-city outputs.
- Keep existing uncommitted `.gitignore` change untouched; work directly on `main` per the user's existing project preference.

## Review Focus

- Missing FRR/NetFlow cells must contribute zero support, never be interpreted as observed normal data.
- Info-only FRR messages must not gain error-level support.
- NetFlow traffic volume alone must not create a direct event or default to `traffic-vm` as root.
- Sparse/low-flow NetFlow protocol-share changes must not dominate root ranking.
- An event whose peak driver cannot be uniquely recovered must be marked ambiguous, never assigned a fabricated trigger.

---

### Task 1: Bounded source-support scorers

**Files:** Create `aiops_v2/detection/source_support.py`; test `tests/v2/test_source_support.py`.

**Interfaces:** Produce `score_frr_support(store) -> np.ndarray [T,N]` and `score_netflow_support(store) -> np.ndarray [T,E]`. Both use masks and return finite scores; NetFlow considers protocol-share and destination-diversity shifts only when flow-record count establishes sufficient observations.

- [x] Write tests with tiny in-memory stores: masked/INFO FRR yields zero, ERR/warning yields bounded positive support; NetFlow volume-only yields zero, sustained sufficiently sampled share change yields bounded positive support; missing/low-flow cells yield zero.
- [x] Run `.venv/bin/python -m pytest tests/v2/test_source_support.py -q` and verify failure due to missing API.
- [x] Implement the two functions with per-edge robust baseline, observation mask, denominator support and explicit relation gating; no mutation of store arrays.
- [x] Re-run the focused test and `.venv/bin/python -m pytest tests/v2 -q`; verify both pass.

### Task 2: Join auxiliary scores to direct-mode RCA

**Files:** Modify `aiops_v2/run.py`; test `tests/v2/test_run.py` and `tests/v2/test_root_ranking.py`.

**Interfaces:** A private direct-mode timeline builder accepts `DirectEvidence` plus source-support arrays. `timeline.log` is FRR support; `timeline.edge` combines service symptoms and NetFlow support for RCA. `global_probability` remains solely the current direct-node/service-event signal; NetFlow alone never opens an event.

- [x] Add a failing integration test: FRR ERR raises the root's `log` explanation; sampled NetFlow shift raises the source root's `netflow` explanation; volume-only does not open an event. Existing direct-mode tests cover scrape masking.
- [x] Run the focused tests and verify expected failure on zeroed timeline components.
- [x] Refactor the direct-mode timeline construction to pass support arrays, preserving array shapes and decoder event constraints.
- [x] Re-run focused tests and `.venv/bin/python -m pytest tests/v2 -q`; verify green.

### Task 3: Read-only event provenance audit

**Files:** Create `aiops_v2/evidence_audit.py`; modify `aiops_v2/run.py` only for an `audit` subcommand; test `tests/v2/test_evidence_audit.py`.

**Interfaces:** `audit_run(store, predictions_path, inference_path) -> dict` returns summary plus compact per-event traces. CLI: `python -m aiops_v2.run audit --store STORE --predictions PREDICTIONS --inference-log INFERENCE --output AUDIT_JSON`. Output is outside the official submission file. Record the peak driver from candidate node/service scores, observed raw value and mask for dominant category metrics, Top1 local direct/log/NetFlow support, and classification-anchor status. If signals tie or data are absent, mark the driver `ambiguous` or `undetermined`.

- [x] Add failing tests for disk-classification conflict, missing metric, two equal peak drivers, output summary counts and no mutation of the submission JSONL.
- [x] Run `.venv/bin/python -m pytest tests/v2/test_evidence_audit.py -q` and verify expected failure on missing API.
- [x] Implement the audit module and CLI with streaming JSONL input, schema checks and atomic audit JSON output; do not reparse raw CSV.
- [x] Run the focused tests and `.venv/bin/python -m pytest tests/v2 -q`; verify green.
- [x] Audit the saved eight-city output into a new ignored `outputs/v2_0/evidence_chain_audit_20260924/` directory; compare measured counts with the pre-change snapshot without treating disagreements as ground truth.

### Task 4: Acceptance and handoff

**Files:** Update `docs/plans/model-v2-true-rebuild.md` with results only if run evidence is available.

- [x] Run all v2 tests and the full project suite; inspect failures rather than hiding them.
- [x] Re-run direct prediction from the existing eight-city feature store into a new versioned output directory, then audit it; keep the old 457-event output untouched.
- [x] Compare before/after event intervals, source support, root and category distributions; report remaining unsupported disk classifications explicitly.
- [x] Review diff for unrelated changes and verify `.gitignore` is unstaged; report verified results and limitations. Do not push.

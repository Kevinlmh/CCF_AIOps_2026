# v2.0 根因—类别一致性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove two verified evidence-amplification mechanisms while preserving direct event detection and official prediction schema.

**Architecture:** Apply bounded, physically meaningful disk semantic scoring in classification; treat repeated traffic edges as one city-level symptom in ranking. Compare predictions and read-only audits from the same frozen eight-city feature store.

**Tech Stack:** Python 3.11+, NumPy, pytest, existing v2 feature store.

**Spec:** `docs/superpowers/specs/2026-09-24-v2-causal-consistency-design.md`

## Global Constraints

- Do not alter event decoding, direct probability, masks, source ingestion, official JSONL schema, or current original output files.
- Do not fit to unlabeled event counts or use public sample labels for parameter selection.
- Preserve the existing unrelated `.gitignore` modification and all prior uncommitted evidence-chain work.
- Work in the user's current `main` checkout, following their earlier explicit preference; do not push or merge.

## Review Focus

- A real high-utilization disk event must retain a strong disk signal and the public disk sample's category.
- Low-utilization disk jitter and disk read/write rate spikes from a near-zero baseline must not overwhelm a strong local CPU signal.
- One or many traffic edges targeting a city must not become many independent votes for each service VM.
- A traffic-only case must still put the target city's service VMs ahead of unrelated observers.
- No change to event intervals, and no silent inference-log/official-output schema drift.

---

### Task 1: Range-aware disk semantic evidence

**Files:** `aiops_v2/classification/semantic.py`, `tests/v2/test_classification.py`.

**Interface:** Keep `extract_candidate_signals()` and `classify_event()` signatures. In both candidate and fallback signal extraction, compute disk utilization's relative anomaly times a continuous physical-range factor, and cap disk read/write relative-only support below a strong utilization signal.

- [x] Write failing synthetic tests: a low disk-utilization rise plus CPU pressure classifies as CPU; a high sustained disk-utilization rise still produces strong disk signal; a read-rate spike from zero cannot independently classify disk pressure.
- [x] Run focused tests and confirm expected assertion failures.
- [x] Implement one shared helper for utilization range strength in both extraction paths; leave read/write rates as auxiliary direct-detector evidence, not independent semantic category signals.
- [x] Run focused and full tests; compare public three-sample output with saved baseline using the existing sample store.
- [x] Reviewer follow-up: verify the rate-only case fails before the fix, then remove it from independent category mapping and make no-evidence fallback confidence zero; rerun the focused test.

### Task 1b: Preserve absolute signal strength in category aggregation

**Files:** `aiops_v2/classification/semantic.py`, `tests/v2/test_classification.py`.

- [x] Write failing real-classifier fixture: a high-root-weight node with weak disk jitter must not outvote another node with a strong CPU change solely because both candidates' categories are normalized to one.
- [x] Run focused test and confirm the old root-prior term produces the wrong disk category.
- [x] Multiply the root-prior vote by each candidate's absolute semantic strength before normalization; retain the evidence-only vote and public taxonomy contract.
- [x] Run focused and full tests, then public samples and same-store audit after all tasks.

### Task 2: Non-additive target service support

**Files:** `aiops_v2/localization/ranking.py`, `tests/v2/test_root_ranking.py`.

**Interface:** Keep `extract_candidate_evidence()` and `rank_root_causes()` signatures; for each target-city service VM, `traffic_target` is the maximum matching edge contribution in the event rather than the sum.

- [x] Write failing tests where multiple equal traffic edges cannot multiply either target VM support or same-source traffic-vm observer support and displace stronger direct device evidence.
- [x] Run the focused test and confirm failure on additive behavior.
- [x] Change both traffic-target and traffic-observer aggregation; preserve edge explanations and observed mask.
- [x] Run focused and full tests, including existing traffic-only and direct-root tests.

### Task 3: Same-store acceptance

**Files:** `aiops_v2/classification/semantic.py`, `tests/v2/test_classification.py`.

- [x] Write failing test: throughput decline alone has no `rate_limit` semantic mapping, while an explicit policer/rate-limit metric still does.
- [x] Run focused test and verify the throughput assertion fails on the current mapping.
- [x] Remove the throughput-to-rate-limit inference without changing the fixed feature schema.
- [x] Re-run focused and full tests.

### Task 4: Same-store acceptance

**Files:** `docs/plans/model-v2-true-rebuild.md` (results only).

- [x] Run full tests and `git diff --check`.
- [x] Predict from `data/feature_store/v2/stage1_all_cities_mac_20260924` to a new ignored directory; run `audit` on that output.
- [x] Compare with `outputs/v2_0/evidence_chain_audit_20260924/verified_*`: event intervals, Top1, labels, disk unsupported count, cross-city conflicts, and key components.
- [x] Run public three-sample direct prediction/evaluation as regression; report evidence and unresolved risks honestly. Do not push.

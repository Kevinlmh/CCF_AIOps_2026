# v3 LLM Diagnosis Upgrade Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement the tasks inline. The user explicitly requested implementation in the existing Mac checkout.

**Goal:** Build an opt-in, evidence-enriched, staged LLM diagnosis workflow with explicit evidence gates and diagnosis ablations.

**Architecture:** Keep detection and official prediction fields unchanged. Add optional device evidence cards to the evidence hash, separate device analysis / ranking / classification requests, and independently accept root and category changes through a deterministic gate. Retain the existing single-request workflow for comparison.

**Tech Stack:** Python >=3.10, NumPy, standard-library HTTP client, existing pytest suite; no model or runtime installation on Mac.

**Spec:** The user's approved in-chat first-stage proposal: retained text and dimension evidence, independent device analysis, separate RCA/classification, evidence-backed adoption, bounded JSON output, and detailed failure audit. Event detection review remains a later experiment.

## Global Constraints

- Work in `/Users/likevin/lmh/CCF_AIOps_2026`, as requested.
- Implement the already validated server schema repair locally; do not invoke a remote model or modify server files in this task.
- Preserve baseline prediction behavior and legacy evidence hashes when new flags are absent.
- Missing observations remain unknown, not zero or healthy. Reference relationships are not verified causal paths.
- New prompts/evidence invalidate cached responses through existing hashes.
- Official JSONL fields and event windows remain unchanged. Root/category ablations are recorded separately.

## Review Focus

- An observer without direct device evidence must not become Top1 merely by citing its probe's business symptoms.
- A category change must cite evidence for that category at the selected root (or verified target-city business symptoms), rather than unrelated signals.
- Dimension-series gaps, normal reference observations, and recovery must be distinguishable in evidence cards.
- Truncation and HTTP errors must be audited with actionable details; incompatible caches must fail before appending.
- Device analysis must cover candidates once, and malformed intermediate output must not be promoted to accepted diagnosis.

## Tasks

1. Schema compatibility and importer hardening: remove `uniqueItems`, reject repeated/unhashable evidence IDs safely, bound evidence arrays and reject truncated completions. Write failing boundary tests, implement, run focused tests.
2. Optional evidence cards: store-level retained text access, masked before/during/after summaries and dimension timelines, source availability, hash/serialization round trip. Write synthetic fixtures, implement, verify legacy behavior.
3. Staged workflow and evidence gates: device analysis without rule answers, ranking with short evidence IDs, separate closed-set classification, field-level fallback and application modes `both`, `roots-only`, `category-only`. Test accepted and unsupported changes, intermediate failures, cache resume and replay integration.
4. Diagnosis ablation utility and documentation: align events by batch-local IDs and intervals, create validated roots-only/category-only merged outputs without model calls, reject ambiguous or mismatched inputs. Document exact server experiment commands and fresh output directories.
5. Run complete local test suite, review the diff and CLI help, and report the limits of verification. No official submission or model inference is performed.

## Implementation Status

- Implemented schema compatibility, duplicate citation validation, truncation detection and HTTP diagnostics.
- Implemented optional device timelines, text citations, source availability and backward-compatible evidence serialization/hashes.
- Implemented opt-in staged requests, field-level adoption audit and replay application modes.
- Implemented aligned field ablations, input/output provenance and the server experiment handoff in `docs/v3-llm-staged-experiment.md`.
- During implementation the focused tests and then the full local suite passed (128 tests). Subsequent manual review added compact request byte limits, explicit complementary metric priority, contiguous recovery handling and the ablation CLI. CLI entry points and whitespace checks were inspected; these final additions have not been exercised against a real vLLM model or hidden scoring service.
- No server synchronization, model inference, official submission or feature-store rebuild was performed.
- Independent static review identified missing incident-window observations being treated as normal and absent citation bounds at import. Both were repaired; cache validation also now covers rows outside a `--limit` subset. Final changes require runtime verification on the server.
- The reviewer inspected those repairs and found no regression in them. This is a static review verdict, not a claim that the final version passed a fresh complete runtime suite.

## GitHub Delivery Follow-up

The user subsequently requested committing and pushing the Mac changes to GitHub `origin/v3`, then updating the server from that branch. The handoff now documents Mac commit/push, existing server fetch/fast-forward pull, the two previously hand-edited schema files, commit SHA comparison, and first-time clone. Server updates remain commands for the user to run; no remote server execution is part of this follow-up.

# v4 State Discovery Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans, sequential TDD and one fresh read-only whole-change reviewer. User already requested execution of the accepted milestone; continue without repeated permission gates.

**Goal:** Traceable feature vectors, robust references, lightweight states and rule candidate events.
**Architecture:** Published v4 windows → native-grain vectors → temporary SQLite groups → reference/medoids → states/events → exclusive artifacts.
**Tech Stack:** Python >=3.10 standard library, pytest.
**Spec:** docs/superpowers/specs/2026-10-08-v4-state-discovery-design.md

## Global Constraints

- Existing data/output/outputs/submit.py unchanged; new output outside input, v4 retained.
- No LLM, labels, old caches, cross-batch models or automatic fault classification.
- Max 128 features, 256 samples per feature, 64 clustering points, 16 categories; explicit insufficient states.
- Reference min 12, cluster k3/iterations6, overlap0.5, statistical6/cluster3/rare0.1; configurable numerical experiment parameters.
- No zero fill, fake windows, gap bridging, binary state code means or unknown metric differences.

## Review Focus

- Sparse/disjoint masks must not produce false zero distances or healthy status.
- Later observations must not change a explicitly bounded reference model.
- Auxiliary entities, different peers/interfaces and categorical states retain their grain.
- Duplicate/overlapping windows, malformed/mixed batches and failed publication preserve input/output.
- Bounded reservoirs/category/features remain bounded; event count is not fault count.

## Task 1: Native-grain feature vectors

Files: aiops_v4/states/{__init__,matrix}.py, tests/v4/test_state_matrix.py.
Interfaces: window_vector(window:dict,batch:str)->dict with group_id, values/descriptors, quality, provenance and rule_inputs.
- [x] Tests: gauge/log/rate/sum/category selections, unknown/missing/conflict exclusion, peer/aux isolation and invalid batch/time.
- [x] Run `.venv/bin/python -m pytest tests/v4/test_state_matrix.py -q`; expected missing module failure.
- [x] Implement window_vector; all tests and full suite green; commit.

## Task 2: Robust references and mixed k-medoids

Files: aiops_v4/states/{reference,cluster}.py, tests/v4/test_state_models.py.
Interfaces: fit_reference(rows,min_reference=12,capacity=256)->dict; normalize(row,reference)->dict; fit_clusters(points,k=3,minimum_overlap=0.5,iterations=6)->dict; assign(values,clusters,minimum_overlap=0.5)->dict.
- [x] Tests: hand-checked median/MAD and constant fallback, insufficient reference, unseen categorical value, bounded samples, missing-mask distances, deterministic medoids and fewer states.
- [x] Run test file; expected missing module failure.
- [x] Implement functions, validate finite config and preserve sparse status; full suite green; commit.

## Task 3: Signals, candidate events and CLI

Files: aiops_v4/states/{signals,events,pipeline,cli,__main__}.py; tests/v4/test_state_pipeline.py.
Interfaces: assess(row,reference,clusters,mode,thresholds,previous)->dict; EventBuilder.push(row,state)->completed events, finish()->final event; discover_states(windows_dir,batch,output_dir,*,reference_start=None,reference_end=None,min_reference=12,clusters=3,mode='hybrid',...)->summary; CLI `python -m aiops_v4.states discover ...`.
- [x] Integration tests: held-out spike, reference-bound exclusion, rules without reference, quality-only no events, adjacency/gap/normal boundaries, all modes, input ordering, duplicates/overlap/mixed batch/invalid JSON, failed output protection.
- [x] Run tests; expected missing module/entry failures.
- [x] Implement SQLite sorting, per-group fitting/scoring, metadata/provenance and exclusive summary-last publication; full suite green and CLI outside checkout; commit.

## Task 4: Measured experiments, review and delivery

Files: README.md; docs/v4-state-discovery-2026-10-08.md; docs/v4-state-discovery-progress.md.
- [x] Public full sample with explicit earlier reference + later assessment, and second-batch prefix; verify every state/event boundary/ref/capacity and protected files.
- [x] One fresh whole-change reviewer; material findings get regression RED→GREEN in one fix pass; no re-review.
- [x] Record synthetic statistics/cluster comparison, actual counts, unavailable references, limitations and next LLM milestone; tests/diff clean; commit.

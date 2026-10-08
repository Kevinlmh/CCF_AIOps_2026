# SDD ledger — plan: docs/superpowers/plans/2026-10-08-v4-evidence-bundles.md

User asked design audit then next coding task. Base52cef70; clean v4 checkout,129 tests passed. Protected snapshot /tmp/aiops-v4-evidence-protected.json:453 files.

Design audit confirms staged mainline; current native references are a documented refinement, LLM/exports remain later. Topology whitelist exists without trusted links.
Ruling: keep current user-selectedv4 and execute inline — no concurrent implementers, local raw data remains accessible; authorization overrides repeated artifact approval gates.
Ruling: topology is an explicit provenance-bearing adapter, not fabricated role edges — missing real link data stays visible; no propagation/root-cause inference.
Ruling: association bundles are review contexts, not merged fault intervals — native membership persists, transitive overlaps can contain multiple hypotheses.
Pre-flight: Task1 packet/grain IDs consumed by Task3 indexed events/states; Task2 verified RawRecord matches producer ID/schema and feature time strategy; Task3 disk tables supply bounded support/counter/quality rows to Task1. Interfaces consistent.

Task 1: RED 8 missing-module failures; GREEN full suite 137/137. Streaming strict-overlap bundles, native-grain bounded packets and provenance-bearing topology validation implemented. Full members will be assigned from indexed original events in Task 3.

Task 2: Initial test collection import corrected before RED; RED 9 missing-module failures; GREEN full suite 146/146. Disk-sorted references, single pass to maximum selected row, content/physical-line verification, complete log preservation and explicit observed_time implemented.

Task 3: RED 14 missing implementation/entrypoint failures. First integration exposed that empty explicit reference intervals legitimately have reference.group_id=None; validation now accepts this only for zero observed rows with insufficient_reference. GREEN full suite 163/163; producer-driven tests include 37-member paged bundle, raw/on-demand anchors, long FRR, prefix scope, direct topology overlap, mutation rejection and publication rollback/read-only invariance.
Task 3: Ruling: retain all state anchors in a disk index, while materializing only packet anchors — allows role tools to retrieve references omitted from compact packets without rescanning the whole dataset; cost is a larger SQLite index.

Task 4 experiments: public 71,208 states / 1,873 events / 695 bundles / 9,161 materialized refs, 19.08s 44.75MiB; stage2 prefix 96,290 / 5,391 / 3,637 / 29,433 refs, 22.17s 56.97MiB. All member and candidate-window conservation, packet capacities, largest bundle pagination, raw on-demand retrieval, SQLite integrity/FK and readonly hash checks passed; 453 protected file metadata unchanged. Detailed new verification.json recorded.

Final review: one fresh read-only gpt-6.1-sol reviewer (working available model; prior gpt-6-astra quota unavailable), 4 Important findings, no Critical/Minor. All regraded Important.
Final: fixed event-local anchor ownership — test_event_cannot_borrow_a_registered_anchor_outside_its_members (other entity and nonmember time) RED→GREEN.
Final: fixed linked-window/matrix provenance — test_genuine_other_run_summary_cannot_relabel_matrix_provenance RED→GREEN; disk temporary vectors bind full imported matrices to genuine linked windows.
Final: fixed total packet anchors including nested observation refs — test_packet_total_anchor_budget_includes_nested_observations RED→GREEN; retained anchors exactly cover materialized snapshots.
Final: fixed role-visible reference/trigger policy — test_role_queries_expose_reference_scope_and_trigger_configuration and test_role_provenance_preserves_offline_and_nondefault_trigger_policy RED→GREEN.
Final: Ruling: reject pre-fix evidence contract version1; new packets/summary/SQLite use version2 — old experimental packets retain the reproduced nested-reference defect and cannot serve as valid role inputs; cost is rebuilding old evidence into new directories (no original data changes). test_old_packet_contract_database_is_rejected_without_rewriting RED→GREEN.
Final: Ruling: future LLM diagnosis/export/accuracy remains outside this milestone — current bridge deliberately supplies review evidence only; cost is no diagnostic score until later roles/evaluation are implemented.
Final: Ruling: full stage2 throughput/distribution not inferred from prefix runs — preserve actual scope; cost is later full-data experiments needed.
Final fixes: one pass, full suite 170/170; no second reviewer. One nested-cap test expectation was narrowed to the actually capped br1 fixture (br2 has fewer than32 anchors). No deferred minors.

Task 4: complete. Fresh corrected contract2 runs: public 24.82s / 46.64MiB, stage2prefix36.30s / 55.98MiB, same event/bundle/materialized-reference counts. New verification-reviewed.json confirms complete/nested packet anchor cap32, all retained anchors materialized, provenance visible to packet/state/model tools, member conservation/paging, on-demand raw verification and DB hash invariance. 453 protected existing files plus both pre-review experimental artifacts unchanged. v3 tag peeled commit still5e69701053ff41ce0fc83a47311c280c71cd856e.
Finish: retain user-selectedv4 and workspace; no merge/push/PR requested. Independent review fixed in one TDD pass; no deferred findings. Full completion check .venv/bin/python -m pytest -q and git diff --check.

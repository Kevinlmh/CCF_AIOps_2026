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

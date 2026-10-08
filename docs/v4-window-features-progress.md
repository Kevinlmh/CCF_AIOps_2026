# v4 windows ledger — plan: docs/superpowers/plans/2026-10-08-v4-window-features.md

User approved next milestone and requested implementation. Sequential work in v4, base ec0629a. Existing file metadata snapshot: /tmp/aiops-v4-windows-protected.json (376 files).

Pre-flight: Task 1 semantics/time/identity consumed by Task 2; Task 2 ordered normalized dictionaries produced by Task 3 temporary SQLite sort; signatures and scope consistent. User selected v4 checkout is retained.

Official website data/rules read through browser on 2026-10-08: stage2 omits detailed flow metrics and FRR; preprocessing permitted, stages isolated. Full CSV units/timezone are not explicitly documented there; evidence and unresolved semantics must be labelled.

Time corroboration: all 477 public traffic records have Unix last-batch times; 474 are within ten minutes of CSV time under UTC and zero under Asia/Shanghai. This supports UTC, does not prove every source timezone.

Task 1: complete — semantic/time/auxiliary policies, snapshot routing counts exempted from generic _count rule; full suite 67 passed.

Task 2: complete — bounded series windows and provenance, missing/invalid/duplicate/conflict distinctions, counter breaks, peer grains, additive NetFlow quantities and bounded quantiles; full suite 75 passed.

Task 3: complete — temporary SQLite disk sort, cross-file counter continuation, exclusive publication, rejected evidence, semantic exports and CLI; full suite 85 passed, CLI invoked outside checkout.

Task 4: real builds complete — public sample 604,284 records / 71,208 windows; stage2 per-file1000 prefix 120,000 records / 96,290 windows. No rejected timestamps. Checked all 167,498 windows for batch, reference paths/limits/line ranges, and bounded quantiles; independently summed raw NetFlow quantities and confirmed conservation. Existing 376 protected files unchanged by size/mtime.

One fresh read-only whole-change reviewer assessed ec0629a..1140ab1. One Important finding: explicit auxiliary traffic identities could be overwritten by source-role inference in the raw reader, contaminating candidate counter chains. Two regression tests first failed (auxiliary mapped to traffic-vm; false delta=15), then passed after restricting inference to absent explicit names. Full suite 87 passed. No deferred material findings.

Review decisions: inferred units and naive-timezone assumptions remain explicitly unverified, not claimed as official confirmation. Actual build/provenance/memory checks were performed by the implementer. Clustering, rules and LLM roles are intentionally later milestones. Following the reader correction, the public full sample is rebuilt into a new reviewed directory; stage2 has no traffic source and the changed branch is not exercised there.

Task 4: complete — final reader revision reran the full public sample (29.35 s, maximum RSS 46,481,408 bytes). windows.jsonl SHA256 matches the initial full build exactly. Rechecked final public plus stage2 prefix: 167,498 windows, bounded references/quantiles, public raw NetFlow sum conservation, and all 376 protected file metadata unchanged. Fresh full suite: 87 passed; git diff --check clean. Delivery report and README updated; v4 retained, v3 archive untouched.

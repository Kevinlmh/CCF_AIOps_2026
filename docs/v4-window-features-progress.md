# v4 windows ledger — plan: docs/superpowers/plans/2026-10-08-v4-window-features.md

User approved next milestone and requested implementation. Sequential work in v4, base ec0629a. Existing file metadata snapshot: /tmp/aiops-v4-windows-protected.json (376 files).

Pre-flight: Task 1 semantics/time/identity consumed by Task 2; Task 2 ordered normalized dictionaries produced by Task 3 temporary SQLite sort; signatures and scope consistent. User selected v4 checkout is retained.

Official website data/rules read through browser on 2026-10-08: stage2 omits detailed flow metrics and FRR; preprocessing permitted, stages isolated. Full CSV units/timezone are not explicitly documented there; evidence and unresolved semantics must be labelled.

Time corroboration: all 477 public traffic records have Unix last-batch times; 474 are within ten minutes of CSV time under UTC and zero under Asia/Shanghai. This supports UTC, does not prove every source timezone.

Task 1: complete — semantic/time/auxiliary policies, snapshot routing counts exempted from generic _count rule; full suite 67 passed.

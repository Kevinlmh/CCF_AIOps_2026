# SDD ledger — plan: docs/superpowers/plans/2026-10-08-v4-state-discovery.md

User authorized next coding milestone. Base 337a120. Sequential native implementation on user-selected v4 checkout; no concurrent implementers. Protected snapshot /tmp/aiops-v4-state-protected.json: 400 files.

Ruling: retain current v4 checkout — user explicitly selected this branch and continued work there; a new checkout would omit local data and is unnecessary for one implementer.
Ruling: no new design approval gate — user approved the architecture and explicitly requested this next milestone; developer requires finishing authorized work before further approval requests.

Pre-flight: Task1 window_vector descriptors and masks consumed by reference/normalize in Task2; Task2 values/medoids consumed by assess in Task3. Task3 sorts all vectors before fitting and applying per group, so input order cannot alter reservoir order. Task4 consumes exact exclusive artifacts. Interfaces consistent.

Ruling: use native-series references rather than pool different devices' throughput — preserves workload and peer/target boundaries; costs insufficient reference states for short series, which are reported rather than guessed.
Ruling: first-batch unpacking is independent of this delivery — current public full sample and second-batch prefix suffice for code verification; neither is claimed to validate the whole competition batch.

Task 1: complete — RED 7 missing-module failures → GREEN 94 total tests; commit 23cbc2a. Native-grain masked vectors preserve provenance and categorical last values.
Task 2: complete — RED 11 missing-module failures → GREEN 105 total tests; commit 470cbcd. Bounded reference reservoirs, explicit MAD fallback and deterministic mixed-mask medoids.
Task 3: complete — RED 18 missing pipeline/CLI failures → GREEN 123 total tests; commit 8a2af48. Explicit reference bounds, three signal modes, observation/quality rules, bounded event evidence, exclusive artifacts and CLI outside cwd.

Task 4: in progress — real builds use explicit earlier reference intervals, not known-healthy claims. One fresh read-only review covers 337a120..8a2af48. Synthetic held-out spike passed all three modes; rules remain configurable and independently counted.

Real initial builds: public 71,208 states / 3,106 groups / 2,990 ready models / 1,873 candidate events; stage2 prefix 96,290 / 2,871 / 660 / 5,391. Independently checked all 167,498 states and 7,264 events, exact reference membership counts, capacities, adjacency, candidate membership/count conservation and raw file references. All 400 protected file metadata unchanged.

Synthetic two-state holdout: reference twelve10/twelve30; holdout70. Median20/MAD10 gives statistical5<threshold6, nearest-medoid distance4>threshold3. Rules disabled: statistics0/cluster1/hybrid1 candidate events. This demonstrates complementarity in a controlled example, not competition accuracy.

Final review: first requested model hit quota before reviewing; a single available fresh reviewer completed read-only review. Three Important, no Critical/Minor. All fixed in one pass, each regression observed RED→GREEN: unused JSON exponent overflow rejected (2 tests); held-out semantic drift rejects false normalization (1 test); recovered within-window binary zero and adjacent recovery evidence retained (3 tests). Full suite 129 passed. No re-review.

Final: Ruling: binary recovery retained as change evidence, not a standalone trigger — otherwise a recovered availability window would extend the unavailable candidate interval; min/last zero still trigger, within-window changes remain in rule_signals. Tests pin both transient zero and adjacent recovery.
Final: Ruling: accuracy/root-cause validity outside this milestone — results remain candidates requiring confirmation; cost is uncalibrated candidate volume, explicitly reported.
Final: Ruling: full-batch performance unverified — actual results are public full sample and stage2 prefix only; no whole-batch scalability claim.
Final: Ruling: author-reported totals/protected files were verified independently against artifacts/metadata by the implementer, including every state/event; reviewer did not repeat those scans.

Task 4: complete — reviewed final code rebuilt both datasets into new -reviewed directories. Model/event SHA256 unchanged; all 167,498 state scoring/trigger/reference fields unchanged; real input did not contain the transient binary recovery pattern, which is covered by regression. Public9.92s/RSS43,630,592 bytes, prefix21.28s/RSS46,006,272 bytes. Protected400 unchanged. Fresh full suite129 passed, diff check clean. Keep current v4, no push/merge, v3 archive retained.

# SDD ledger — plan: docs/superpowers/plans/2026-10-09-v4-experiments.md

Baseline: v4 7545eaa; 275 passed in 11.78s. Protected 543 existing files in /tmp/aiops-v4-final-protected.json; no AGENTS.md. Docker CLI installed, daemon socket absent. LLM configuration env absent; no credential contents read.

Pre-flight: Task 1 → Task 2: public knowledge dict and provenance prompt inventory; compatible.
Pre-flight: Task 1/2 → Task 3: validated normalized config and audited new-directory role runner; compatible.
Pre-flight: Task 3 → Task 4: sealed completion marker and independent raw/config fingerprints; compatible, evaluation never passed into inference.
Pre-flight: Tasks 1–4 → Task 5: JSON-compatible summaries and path normalization; compatible.
Pre-flight: Tasks 1–5 → Task 6: replay flag, partial scope, role traces and immutable protected snapshot; compatible.

Ruling: Reuse explicitly requested v4 and execute inline without repeated design approval — user has authorized this final batch and developer prioritizes completing authorized work — cost if wrong: design changes need another commit.
Ruling: Official technical-requirements table forbids pure-rule substitution while FAQ allows broad methods; retain hybrid LLM default and mark discovery-only non-submittable rather than deciding the site's contradiction — cost if wrong: official interpretation still requires organizer confirmation.
Ruling: No configured model/key and unavailable Docker daemon are validation limitations, not reasons to leave runnable code unfinished; report actual replay and container evidence accurately — cost if wrong: live-provider or Docker-runtime incompatibility may only appear in later runs.
Task 1: complete (commits cf9a456..5c96507, tests: .venv/bin/python -m pytest -q → 292 passed in 11.59s)
Task 2: complete (commits 5c96507..3fed487, tests: .venv/bin/python -m pytest -q → 299 passed in 11.96s)
Task 3: complete (commits 3fed487..5245624, tests: .venv/bin/python -m pytest -q → 306 passed in 12.14s)
Task 4: complete (commits 5245624..95cca40, tests: .venv/bin/python -m pytest -q → 312 passed in 13.38s)
Task 5 container evidence: Docker Desktop CLI started installed daemon successfully; docker info linux 29.7.2. Native GUI was unavailable due to locked Mac. First runtime build failed fetching docker.io/library/python:3.12-slim metadata (DeadlineExceeded). One direct pull attempted after daemon became ready; no registry/security settings changed.
Task 5: complete (commits 95cca40..ffdadc0, tests: .venv/bin/python -m pytest -q → 315 passed in 13.99s)
User steering: 暂时不要考虑使用docker. Stop all Docker/container work immediately, including the owned host image-download helper. Local verification and whole-project review continue; existing container scaffolding is inactive and not a completion requirement.
Ruling: Remove Docker runtime validation from this completion gate per latest explicit user instruction; retain already-written dormant files without further container work — cost if wrong: container runtime compatibility remains unverified.
Final review: fresh gpt-6-astra reviewer completed all v4 modules and 315/315 tests; regraded four Important findings by publication/export/isolation effects, no Critical. One Minor deferred: KeyboardInterrupt leaves no failure.json, but never publishes success.
Final: minor (deferred): Catchable Ctrl-C lacks sanitized failure.json; completed stage files remain and no success marker is written. SIGKILL cannot guarantee a record.
Final: Ruling: Stronger run integrity_version=2 and role run_schema_version=2 require fresh runs for verified scoring/export; preserve old artifacts without upgrading their claims — cost if wrong: old experiments must be rerun in new directories.
Final: Ruling: Direct diagnosis APIs may inspect a supported event, but accepted strict/experimental export requires its real confirmation record; no synthetic confirmation calls — cost if wrong: direct callers must supply a recorded confirmation block.
Final: Ruling: Real model accuracy, causal quality, calibration and multi-role score benefit remain unmeasured; executable architecture is complete only after fixes, Replay stays simulated — cost if wrong: later model experiments can show poor performance.
Final: Ruling: Live provider tool support, model alias drift, limits and billing are not inferred from loopback/replay contracts — cost if wrong: actual integration or cost may differ.
Final: Ruling: Stage1/stage2 full scale and quality are not inferred from per-file prefixes; full experiments remain next work — cost if wrong: full candidate count/resource use may exceed local budgets.
Final: Ruling: Parent real-data runs are checked directly in artifacts and repeated after the fixes; do not describe them as reviewer independently rerun — cost if wrong: verification is still by the implementation owner.
Final: Ruling: Thresholds, cluster count, reference selection and missing policy are configurable starting points, not optimal settings — cost if wrong: polluted references or unsuitable thresholds reduce detection quality.
Final: Ruling: Unconfirmed timezones/units/reset semantics and absent physical topology stay explicit assumptions/unknowns — cost if wrong: current assumptions can miss signals or shift timing.
Final: Ruling: No speculative cross-bundle fault merger added before error analysis shows the need — cost if wrong: the same physical fault can produce extra predictions/FP.
Final: Ruling: Official pure-rule/API/deployment interpretation and submission acceptance remain organizer questions; do not execute submit.py — cost if wrong: eventual official environment can require runtime changes.
Final: Ruling: Credential contents, private labels and dormant v1-v3 correctness are outside this v4 review; preserve protected files and archive — cost if wrong: they carry no new correctness guarantee.
Final: Ruling: Local hashes catch accidental/uncoordinated edits, not an attacker rewriting artifacts and seals together — cost if wrong: authenticated provenance needs a separate signing design.
Final: Ruling: Docker runtime remains outside the current gate under explicit user pause — cost if wrong: container compatibility has no evidence.
Final: fixed stage publication integrity — test_previously_published_stage_cannot_change_during_agents (3 mutations) RED→GREEN, suite 339/339.
Final: fixed external replay/topology evaluation isolation — test_consumed_external_input_cannot_be_reused_as_truth (4 path/inode cases) RED→GREEN, suite 339/339.
Final: fixed confirmation and recorded role/evidence revalidation — direct unsupported events and trace/count/final/proof/confirmation mutations in both modes RED→GREEN, suite 339/339; full chunk tool reconstruction also passed.
Final: fixed actual relation delivery — test_every_actual_role_prompt_delivers_registered_relation_ids (independent/joint; actual confirmation/diagnostic/review requests) RED→GREEN, suite 339/339.
Task 6 real verification after ab54f4d: public full 56 files/604284 rows/71208 windows, 4 Replay calls, review defer/0 predictions; stage1 prefix 56 files/51481 rows/41095 windows, stage2 prefix 120 files/120000 rows/96290 windows, each selected1 bundle/1 missing Replay call/0 predictions. All three verified integrity_version=2; public sealed partial contract eval Total0/FN3, never model quality.
Protected validation: 543 existing files unchanged by existence/size/mtime, representative existing evidence SHA and v3 archive commit unchanged. No existing outputs overwritten.
Final: Ruling: Keep the existing requested v4 branch locally; no integration menu/push/merge because no integration was requested — cost if wrong: integration remains a later explicit task.
Task 6: complete (commits ffdadc0..a777234, tests: .venv/bin/python -m pytest -q → 339 passed in 16.58s)
Finish: v4 kept locally; all implementation changes and audit documents committed. Only this plan-owned scratch is removed; experiments/protected files and other plan workspaces remain.

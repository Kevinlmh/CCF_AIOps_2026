# v4 Compact Storage and Candidate Selection Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in this session. User authorized direct project and server changes, verification and deletion of the obsolete prefix run.

**Goal:** Reduce disk use and weak candidate proliferation, then run all three public cases with the local model and official evaluator.

**Architecture:** Compress machine artifacts without changing their logical hashes or query results. Stream NetFlow into bounded window accumulators (sum and moments, no per-flow quantile sampling). Add an explicit conservative candidate policy before event construction, retaining suppressed states for context.

**Tech Stack:** Python standard library, gzip/zlib, SQLite, pytest, local vLLM.

**Spec:** This document and the user's current request.

## Global Constraints

- Keep raw CSV, models, submit.py and previous useful experiment outputs.
- Do not use ground truth during profiling, detection or diagnosis.
- Keep legacy interfaces and default stand-alone behavior; opt into compact/conservative experiment configuration.
- Every candidate window belongs to exactly one event; suppression must precede event construction.
- Retain all states and registered raw references for read-only model tools.
- Preserve existing user changes; no push or submission requested.

## Review Focus

- Unordered/cross-file NetFlow rows: exact sums, moments, counts and earliest bounded references.
- Missing/negative inputs and blocked identities: retain quality and invalid counts.
- Compressed artifacts: decompressed hash matches original logical input, sealing catches corruption.
- Singleton weak anomalies versus protocol interruption: persistence applies only to weak evidence.
- Three cases: evaluate each against its own supplied answer, plus the combined predictions against all three answers; never label-filter predictions.

## Tasks

1. Add failing compact reader/database and streaming NetFlow tests; implement compression and aggregation; verify existing window/trace contracts.
2. Add failing conservative selection tests (small fluctuations, rare-only clusters, logs, persistence, recovery); implement policy and explicit config.
3. Verify complete suite, sync only changed project files, retain a concise old-run record and remove the obsolete prefix run, rerun identical prefix.
4. Start scoped localhost model service, run full public cases, evaluate with official SDK, write repeatable commands and measured results.

## Progress / rulings

- Preflight: server reachable, 60 GiB home disk free, 232 GiB RAM available, model endpoint stopped. Additional disks exist; no shared files touched.
- Ruling: direct execution already authorized; no extra approval pause or GitHub push. Preserve `.dockerignore` / `Dockerfile.v4` user deletions.
- Tasks 1–2 complete: failing compression/NetFlow/admission tests observed, then passed. Added direct gzip writes to avoid an uncompressed publication peak, overflow and cache-eviction tests, and legacy database-read compatibility.
- Task 3: local and server suites both 370 passed. Deleted only the obsolete prefix run after preserving its verified summary/config. Interim identical prefix: 41,095 windows, 435 events, 381 bundles, 269.1 MiB with direct gzip publication. The final mechanism-gated prefix is running separately.
- Final independent review: fixed suppression-as-counterevidence and mixed-version sample reuse, both RED→GREEN plus full suite. Additional model probe revealed the AWQ template lacks enable_thinking; generated a service-specific closed-thinking template and verified pure JSON plus parsed function calls.
- Ruling: conservative is designed for the recommended hybrid mode. Pure cluster with rules disabled cannot admit a numeric fault through this policy; use legacy for pure-cluster ablations. This is intentional corroboration, with a cost of lower recall for cluster-only anomalies.
- Public input discovery exposed ordinary interface throughput and disk activity dominating the queue. Interrupted that unscored attempt, retained its summaries and removed its intermediate directory. Added tested pressure/error/protocol corroboration; normal throughput remains queryable context.
- Task 4 in progress: all three full public inputs are running again with local Qwen; no labels supplied during inference, no external submission.
- Public regression exposed reference absorption of sustained pressure, static protocol zeros, context overflow, and malformed model JSON/long-ID omission. Added failing tests then corrected pressure corroboration, static-zero admission, smaller default evidence packet, one recorded JSON repair and an explicit whole-manifest shorthand (coverage/continuity/support validation remains intact). No diagnosis is manufactured. Both unsuccessful attempts were scored transparently, their diagnostic summaries retained and heavy intermediate files removed.
- Final measurements: same 51,481 prefix rows / 41,095 windows, 22 events / 22 bundles, 251.83 MiB, peak RSS 67,192 KiB, wall clock 3:47.29. New sealed run retained; obsolete comparison runs removed after preserving summaries.
- Added explicit optional temperature, default state-tool page size 1, concise output instructions and a sealed-source reuse mode. Cached detector inputs and current diagnoser versions are recorded separately; no CSV rescans or copied large artifact sets in this mode. RED→GREEN tests cover backend payload, small pages, source/config checks, cache resume and corruption.
- Final local and server suites: 384 passed. Package code fingerprints checked equal. Final full-public-input local-model diagnosis selected all 4 bundles, made 10 requests and produced no predictions: 3 invalid JSON outputs and 1 invalid window assignment. Official per-case and combined Total=0, TP=0/FP=0/FN=3; independently rescored with the official CLI. Actual errors and candidate interval Dice retained. This completes the requested storage/candidate changes, sample experiments and commands; the LLM confirmation integration has not passed the public cases and requires a separate structured-output correction before full-batch inference.
- Source candidates/evidence and final small diagnosis records are retained for repeat experiments. No GitHub push or external submission was performed.

## Comprehensive audit follow-up

- User requested direct server investigation and optimization after the zero-score run. Reproduced protocol failures, real context overflow and proof-registration order errors before implementing fixes. Added optional native structured roles with complete short-alias window assignments, delivered-citation validation, deterministic proof registration and independent event scopes. Final compact state query schema and runtime cap pages at one record; legacy interfaces remain compatible.
- Local and server suites both passed 404 tests, including full four-role HTTP/replay/export, unseen references, alias coverage, resource comparison arithmetic and query budgets. All three full source cases remain sealed and are reused without labels during diagnosis; answers are read only after all predictions are generated.
- First successful diagnosis: 48 calls, 3 predictions, TP3/FP0/FN0, official Total 89.3444; two true resource subclasses were still mistaken for CPU pressure. Added independent resource rules control: 0 model calls, 4 predictions, TP3/FP1/FN0, Total 93.3103.
- Resource summaries, reason-first schemas and smaller initial observation samples were actually tried and worsened scores (58.0567, 55.2633, 26.9706). Reverted those behavioral experiments instead of presenting them as improvements. Kept output protocol, scope/proof fixes and one-record query limits.
- Final retained diagnosis: `outputs/v4/official_samples_stable`, 48 calls, 4 predictions, TP3/FP1/FN0, Total 86.6436. All roles completed without protocol errors or deferrals. Official evaluator CLI independently reproduced this score. Memory and disk I/O subclasses still mistaken for CPU, and a monitoring-node false positive remains. This closes the authorized audit, server edits and public regression, but does not establish hidden-set improvement or superiority of LLM diagnosis.
- Audit report and reproducible server commands are in `docs/v4-audit-and-sample-results.md` and `docs/v4-server-experiments.md`. No push, public upload or contest submission. Raw data, model files and useful experiment records retained; only explicitly obsolete intermediate runs were cleaned.

## Original-architecture full-run follow-up

- User explicitly requires the original independent confirmation/localization/classification/review workflow, including actual LLM calls. No resource-rule fallback replaces the production diagnoser.
- Reproduced and fixed lost native counter/state/sum features and peer/interface dimensions in compact role observations; OSPF non-Full interruption suppression, including within-minute recovery; physically invalid initial resource references; and quadratic confirmation group schemas. Each behavioral regression was observed failing before implementation.
- Added validated stage checkpoints/resume, progress callbacks and configurable free-disk reserve. A separate regression showed low-disk control exceptions were being swallowed by the CSV profiler; the final runtime guard now uses a distinct RuntimeError subclass.
- After the fresh raw-to-model run was sealed, synchronized the disk guard and counter-rate unit fixes. Final package code fingerprint is `d1d2b894c7430448322874bfc7dcd084d3d802c5e7d8c776668dc7c01084de29` on both machines. Counter rates now retain their actual per-second unit, with original quantity units separately recorded; unknown units remain unknown. RED→GREEN tests cover native matrix cells and compact projections of old sealed inputs.
- Fresh final verification on 2026-10-11: all 425 tests passed locally in 29.47 seconds and on the server in 68.91 seconds; `git diff --check` was clean. Production code stays unchanged while the full task is running.
- Fresh all-three full-input run: `outputs/v4/official_samples_upstream_review`, producer `7ac81281ece9b6ac3e0d63e0482fa94e15b931e465ca9788488a3fd0b155c6e4`; no reuse of historical candidates. Model service runs on localhost, GPUs 0 and 1 only.
- The fresh public run completed: 1,631,293 rows, 185,789 windows, 4 candidates/bundles, 48 real model calls, 4 predictions, no role failures or deferrals. Official Total 86.643611; independently reproduced by the official evaluator CLI. Paired resource-rules control on these same new candidates scored 93.310278. Misclassification of memory/disk I/O as CPU and a monitoring-node false positive remain; the upstream fixes did not improve these resource-case scores.
- A separate final-version diagnosis regression in `outputs/v4/official_samples_units_review` completed with the newly sealed `upstream_review` candidates; producer and current diagnoser versions remain separate. All 4 bundles completed, 48 actual model calls, 4 predictions, no failures or deferrals. Official Total remained 86.643611, independently rescored with the official CLI. It does not claim another raw CSV rescan.
- Full stage1 config `configs/v4/stage1-local-llm.json` scans all 56 CSV files in one batch, no row or bundle cap, preserves cross-city queries and original roles. Started 2026-10-11 11:54:34 CST in tmux `aiops-v4-stage1-full`; output and temporary files go to the checked user-owned `/data/liminghang/CCF_AIOps_2026` directories. Data disk free space at launch was about 1,455 GiB, runtime reserve is 12 GiB. Peak artifact size remains unmeasured; no complete prediction claim until input and diagnosis coverage checks pass.

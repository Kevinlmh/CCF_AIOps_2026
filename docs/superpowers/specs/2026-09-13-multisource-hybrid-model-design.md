# Multi-source Hybrid Diagnosis Model Design

## 1. Objective

Build the first competition-ready model for the 2026 CCF AIOps challenge by
replacing the public baseline's single-pass five-sigma detector with a
reproducible multi-source diagnosis pipeline.

The model must:

- parse and use every supplied observation family: node metrics, interface
  metrics, routing metrics, scrape health, traffic-flow metrics, NetFlow and
  FRR syslog;
- detect one or more fault intervals from continuous input without reading a
  ground-truth file;
- rank at most five unique public network-element IDs for every event;
- select one valid major/sub-category pair from the public taxonomy;
- run locally in a lightweight development mode and expose a backend for a
  multi-GPU LLM server;
- keep inference deterministic and auditable enough for code and model review;
- retain a stable tensor-ready representation for a later temporal
  Transformer/GNN implementation.

The work intentionally excludes the data-team assignments at the end of
`AIOPS_OnePage.md`. Dataset documentation and manual case attribution are not
deliverables of this model implementation.

## 2. Competition Constraints

The design treats the published rules as model constraints rather than hidden
labels:

- Initial scoring is AD 40, RCA 40, major category 10 and minor category 10.
- A ground-truth incident that does not match a predicted interval receives
  zero RCA and classification credit.
- Interval matching requires Dice overlap of at least 0.4. Boundary accuracy is
  continuously rewarded within a 180-second tolerance.
- False-positive intervals reduce the AD score; duplicate predictions for one
  incident become false positives.
- Root-cause credit decreases by rank: 1.0, 0.8, 0.6, 0.4 and 0.2 for ranks
  one through five.
- Top-five network-element IDs must be valid, unique and ordered consistently
  with their rank fields.
- A fault lasts from one to thirty minutes. Faults are normally separated by at
  least twenty minutes, do not overlap, and the environment is restored after
  each injection.
- The first published competition input is fourteen continuous days containing
  292 faults.
- The inference pipeline may use preprocessing, statistics, machine learning,
  public topology, general operational knowledge and LLMs, but may not contain
  test answers, per-case mappings or manual labels.
- The reviewed submission must be reproducible and retain inference logs.

The duration, separation and non-overlap facts may guide event segmentation.
They must never be used to invent a fixed event schedule.

## 3. Selected Approach

Use a hybrid model with three independent but composable decision layers:

1. A robust unsupervised time-series model detects anomalous evidence and event
   intervals.
2. A topology-aware fusion model ranks candidate root-cause elements from
   temporal, local, relational and cross-source features.
3. A closed-set prototype classifier produces a reproducible local category,
   while an optional LLM backend re-ranks candidates and classifies the event
   from the same compact evidence.

The statistical and prototype paths make local development possible without
GPU dependencies. Competition inference will use the hybrid LLM path so the
final decision is not a pure rule script. General fault-signature knowledge is
permitted, but case identifiers, incident times and sample answers are never
features.

## 4. Non-goals for Version 1

- Training a supervised end-to-end Transformer/GNN from the three public sample
  labels.
- Resolving physical switch IDs that are absent from the public root-cause
  enumeration.
- Inventing exact service-VM identities from a target domain when the input
  does not provide a defensible mapping.
- Online streaming alerts. The implementation is offline but processes large
  files incrementally.
- Replacing the official evaluator or output schema.
- Downloading or bundling model weights.

## 5. Package Boundaries

The implementation will follow these responsibilities:

```text
baseline/bian/
├── preprocessing/
│   ├── observations.py       canonical records and identifiers
│   └── multisource.py         seven source parsers and aggregators
├── anomaly_detector/
│   └── robust_detector.py     robust scoring and event segmentation
├── localization/
│   └── graph_fusion.py        topology-aware candidate model
├── classification/
│   └── prototype_model.py     local closed-set category model
├── models/
│   ├── api_backend.py         OpenAI-compatible JSON backend
│   └── backend.py             local Transformers JSON backend
├── config/
│   └── model_v1.json          documented model parameters
└── run.py                     orchestration and JSONL output
```

Existing evaluator, schema and public taxonomy code remain authoritative.
Existing BiAn ranking and prompt components may be reused where their contracts
remain valid.

## 6. Canonical Data Contracts

### 6.1 Numeric observation

Every numeric signal is represented as:

```python
NumericObservation(
    timestamp: datetime,
    source: str,
    node_id: str | None,
    related_node_ids: tuple[str, ...],
    metric: str,
    value: float,
    dimensions: tuple[tuple[str, str], ...],
    direction: str,
)
```

`node_id` is the network element directly measured by the observation.
`related_node_ids` contains defensible endpoints or candidates affected by a
relational observation. Unknown endpoints stay unknown; they are not guessed.
`dimensions` keeps identity such as interface, route metric, peer, exporter,
protocol, flow type and source/target region.

### 6.2 Text event

FRR messages are represented as:

```python
TextEvent(
    timestamp: datetime,
    source: str,
    node_id: str | None,
    severity: str,
    program: str,
    event_family: str,
    message: str,
)
```

Version 1 uses normalized event families and severity counts for statistical
detection while preserving bounded message excerpts for LLM evidence.

### 6.3 Anomaly evidence

The robust detector produces:

```python
AnomalyEvidence(
    timestamp: datetime,
    source: str,
    node_id: str | None,
    related_node_ids: tuple[str, ...],
    metric: str,
    value: float,
    baseline: float,
    score: float,
    direction: str,
    dimensions: tuple[tuple[str, str], ...],
    summary: str | None,
)
```

Scores are finite and capped. A zero-variance baseline cannot produce an
unbounded magnitude.

### 6.4 Event

```python
DetectedEvent(
    start: datetime,
    end: datetime,
    peak_time: datetime,
    confidence: float,
    evidence: tuple[AnomalyEvidence, ...],
    source_counts: dict[str, int],
)
```

`run.py` will adapt this object to the existing dictionaries expected by
legacy BiAn code only at compatibility boundaries.

### 6.5 Tensor-ready representation

The canonical layer exposes stable indices and masks that can later form:

```text
node_features     [T, N, F_node]
edge_features     [T, E, F_edge]
observation_mask  [T, N, F_node]
edge_index        [2, E]
```

Version 1 does not require PyTorch to materialize these arrays. It guarantees
that timestamp, node, metric and dimension keys are stable and serializable.

## 7. Source-specific Processing

### 7.1 Node metrics

- Keep one series per node and metric.
- Exclude identifier and timestamp columns from numeric features.
- Preserve CPU, load, memory, swap, disk, filesystem, inode, file-descriptor
  and process signals.
- Treat these as direct local evidence for resource faults and supporting
  evidence for firewall or service degradation.

### 7.2 Interface metrics

- Key each series by node, interface ID, interface role and metric.
- Never merge different interfaces into one sequence.
- Preserve byte/packet rates, drops, errors and carrier changes.
- Keep direction metadata so increases in errors and decreases in throughput
  can both be anomalous.

### 7.3 Routing metrics

- Key each series by node, `metric_name` and normalized label.
- Treat the numeric `value` as the measurement; never create one shared
  `routing.value` sequence.
- Preserve peer, interface, prefix and command labels.
- Mark BGP/OSPF session state and command failures as direct routing evidence.

### 7.4 Scrape health

- Key by node, target and exporter type.
- Use `scrape_up`, duration and sample count as data-quality evidence.
- A scrape failure downweights missing metrics from the same node and period;
  it does not automatically make that node the root cause.
- Preserve sanitized scrape errors as text evidence when present.

### 7.5 Traffic-flow metrics

- Key by series key, flow type, source region, target region, target domain and
  protocol.
- Convert cumulative `*_total` counters to per-minute non-negative deltas and
  mark counter resets instead of treating them as negative traffic.
- Use derived latency, QPS, success/error/timeout ratios, throughput, loss,
  retransmission and jitter signals.
- Attribute source-side evidence to the source region's `traffic-vm`.
- Add target-region relational evidence to service candidates without claiming
  an exact service VM unless an explicit mapping is available.

### 7.6 NetFlow

- Stream rows and aggregate by minute, observer node, interface and protocol.
- Produce packet, byte, flow-record, unique-source, unique-destination,
  unique-port and TCP/UDP/OSPF share features.
- Bound cardinality with aggregate counters; raw five-tuples are not stored in
  memory.
- Preserve a small deterministic set of dominant endpoints/ports as evidence
  summaries.
- NetFlow observer anomalies are evidence at routers, while endpoint changes
  remain relational symptoms unless the topology supports causality.

### 7.7 FRR syslog

- Parse event time before receive time and normalize hostnames to public router
  IDs.
- Aggregate counts by minute, node, severity, program and event family.
- Recognize general BGP, OSPF, route, interface, command/configuration and
  process families through reusable patterns, not case-specific messages.
- Preserve only bounded, sanitized excerpts for model prompts.
- Empty syslog files are valid and do not fail inference.

## 8. Robust Anomaly Model

### 8.1 Per-series transformation

- Sort and deduplicate timestamps per full series key.
- Convert eligible cumulative counters to minute deltas.
- Apply `log1p` to non-negative heavy-tailed count/rate features when
  configured.
- Retain explicit missing-value masks. Do not replace missing data with zero.

### 8.2 Baseline

Use a causal rolling window for formal continuous data and a prefix fallback
for short sample cases:

- default rolling lookback: 60 minutes;
- minimum history: 4 valid points for short samples and 15 for long runs;
- center: rolling median;
- scale: `1.4826 * MAD` with a relative and absolute floor;
- optional level-shift score from the difference between short and long rolling
  medians.

The scale floor is derived from local value magnitude and configured metric
families. The final score is clipped to a finite maximum.

### 8.3 Evidence threshold

An observation becomes evidence when at least one condition holds:

- robust deviation exceeds the source threshold;
- a state metric changes to a known degraded state;
- a level shift persists for the configured number of samples;
- a sufficiently severe FRR family event occurs.

Single-source evidence can trigger an event when its score and persistence are
strong. Weaker evidence requires agreement across multiple series or sources.

### 8.4 Minute-level event energy

For every minute, aggregate capped top-k evidence scores instead of summing all
points. This prevents high-cardinality NetFlow and routing data from dominating
node metrics. Source energies are normalized before fusion.

Use hysteresis:

- a higher threshold opens an event;
- a lower threshold keeps it open;
- short internal gaps are bridged;
- inactive tails close the event;
- detected boundaries remain on observed timestamps.

Events longer than thirty minutes are split at the lowest internal energy.
Events separated by less than the configured quiet period are merged only when
the combined duration remains valid and the gap still contains supporting
evidence. No fixed number of events is assumed.

## 9. Topology-aware Root-cause Model

Create one candidate for every valid public network element. Candidate evidence
contains direct and relational observations separately.

Local features include:

- capped anomaly severity;
- persistence and affected metric-family count;
- temporal precedence relative to event start and other candidates;
- number and reliability of independent sources;
- directness: resource/routing state on the measured device versus remote
  traffic symptoms;
- recovery alignment at the event end;
- scrape-health confidence.

Graph features include:

- distance to anomalous nodes;
- number of downstream symptoms explainable by the candidate;
- upstream position on documented intra-region paths;
- agreement between flow direction and topology;
- penalties for symptom-only nodes whose anomaly starts after a stronger
  upstream candidate.

The local fallback score is a normalized weighted fusion loaded from
`model_v1.json`. It returns five unique valid IDs even when evidence is sparse.
Candidates without evidence receive a low prior rather than an arbitrary high
rank.

The LLM receives the strongest bounded evidence for a shortlist, the event
timeline, topology and public taxonomy. It must not receive ground truth,
prediction examples or candidate ordering as a hidden answer cue.

## 10. Closed-set Classification

The local classifier compares event features with 28 public fault prototypes.
Each prototype is a documented vector over general signal families such as:

- resource CPU/load, memory/swap, disk I/O, disk space, process count and
  network pressure;
- interface throughput, loss, errors and carrier state;
- BGP, OSPF, route and default-route state;
- traffic latency, success, error, timeout, throughput and protocol scope;
- NetFlow volume, disappearance, endpoint and port selectivity;
- FRR event families;
- likely root role and affected business type.

Prototype similarity is a fallback development model and an explicit feature
for the LLM. It contains no case IDs, timestamps or answer mappings. The LLM
must output exactly one pair present in the public taxonomy. Multiple LLM
passes may be aggregated deterministically when configured.

## 11. LLM Backends

Define one JSON-generation protocol shared by both backends:

```python
generate_json(
    *,
    role: str,
    prompt_name: str,
    payload: dict[str, object],
    validator: Callable,
    max_new_tokens: int,
) -> dict[str, object]
```

### 11.1 Local Transformers

- Preserve the existing local path/model-ID behavior.
- Use deterministic decoding.
- Select CUDA with `device_map="auto"` so a future checkpoint can use all four
  RTX 5090 GPUs.
- Keep CPU/MPS development possible when dependencies and weights permit.

### 11.2 OpenAI-compatible API

- Configure base URL, model name and API-key environment-variable name through
  CLI/config, never source code.
- Support vLLM's OpenAI-compatible chat-completions endpoint.
- Send no data until the user explicitly configures and invokes this backend.
- Apply timeout, retry and JSON validation.
- Avoid logging credentials or full raw network records.

### 11.3 Development fallback

`--decision-backend local` uses the topology fusion and prototype models. It is
for tests, feature development and reproducibility checks. Submission runs
should use `--decision-backend transformers` or `api` unless the team has
separately validated that the local statistical model satisfies review rules.

## 12. Orchestration and CLI

Keep the existing invocation compatible while adding explicit options:

```text
--detector robust|five-sigma
--decision-backend local|transformers|api
--model MODEL_PATH_OR_ID
--api-base URL
--api-key-env ENV_NAME
--config PATH
--inference-log PATH
--max-events INTEGER
```

Legacy `--use-llm` maps to the Transformers backend with a deprecation warning.

The pipeline writes prediction JSONL only after schema validation. Inference
logs contain event energies, selected boundaries, source counts, candidate
feature components, model backend, category scores and sanitized failure
details. Logs never contain API keys or full NetFlow rows.

## 13. Failure Handling

- Missing observation families produce a warning and source-coverage metadata,
  not a crash.
- Malformed rows are counted and skipped with path and line statistics.
- Non-finite numeric values are ignored.
- Unknown cities/nodes remain relational evidence and cannot create an invalid
  output ID.
- A failed optional LLM call fails closed by default. An explicit configuration
  may permit the local model fallback and records that decision in the log.
- Invalid model JSON is retried within the configured bound and then rejected.
- Output is atomically assembled and schema-validated before replacing the
  requested destination.

## 14. Performance Requirements

- NetFlow and syslog parsers operate incrementally.
- No raw five-tuple corpus is retained after aggregation.
- Series storage is bounded by aggregated minute keys and configured evidence
  limits.
- The three public sample cases must run on the current Mac without installing
  Torch when the local decision backend is selected.
- Formal fourteen-day input must not require loading every CSV row
  simultaneously.
- LLM payloads are compact and capped by candidate/evidence limits.

## 15. Testing Strategy

All production changes follow red-green-refactor.

Unit tests cover:

- public node-ID normalization;
- all seven source parsers;
- interface and routing dimension separation;
- counter delta and reset handling;
- bounded NetFlow aggregation;
- empty and non-empty FRR logs;
- robust score behavior with zero variance and missing values;
- event hysteresis, splitting and quiet-gap handling;
- candidate uniqueness and valid public IDs;
- topology/temporal feature effects;
- all taxonomy outputs and prototype selection;
- API request construction with an injected local HTTP test server;
- credential redaction and model-output validation.

Integration tests cover:

- each public sample case produces exactly one structurally valid prediction;
- the detector reads every available source family and reports its coverage;
- sample inference does not read `ground_truth.jsonl`;
- local development inference is deterministic;
- the official evaluator accepts generated prediction JSONL;
- a synthetic multi-event dataset produces distinct intervals without
  duplicate Top5 IDs.

Ground truth is used only in an explicit evaluation test after predictions are
generated. No production module imports or opens it.

## 16. Acceptance Criteria

Version 1 is accepted when:

1. Tests demonstrate successful parsing and non-zero modeled features for every
   non-empty observation family.
2. FRR syslog is covered by a synthetic non-empty fixture and public empty
   files remain valid.
3. No series key mixes interfaces, routing metric names, peers or traffic-flow
   identities.
4. All anomaly scores are finite and bounded.
5. All three public samples complete in local mode and produce schema-valid
   JSONL.
6. The official evaluator runs successfully against the generated sample
   predictions and a report is recorded without using labels during inference.
7. The inference log proves source coverage, event boundaries and candidate
   score components.
8. The API backend contract is tested without requiring a real external API.
9. Existing baseline compatibility and evaluator tests remain green.
10. Git diff contains no sample-answer mapping, model secret or generated large
    artifact.

Sample score is reported as evidence, not asserted as hidden-test performance.

## 17. Migration to Temporal Transformer/GNN

The later learned model replaces decision layers, not ingestion:

1. Materialize canonical observations as node/edge tensors with masks.
2. Pretrain a temporal encoder on the fourteen-day unlabeled series using
   masked reconstruction or forecasting.
3. Use the version-1 detector and LLM outputs as auditable pseudo-labels only
   after manual leakage review.
4. Replace robust anomaly scoring with a temporal anomaly head.
5. Replace graph-fusion weights with a GNN root-cause head.
6. Add a closed-set category head and retain the LLM as optional reranker or
   explanation layer.

Because raw observations, dimension keys, topology edges and output contracts
remain stable, this migration does not require rewriting the seven source
parsers or evaluator integration.

## 18. Compliance Notes

- Public sample labels are never embedded in production configuration.
- Ground truth is excluded from the inference data root by contract and tested.
- Public taxonomy, topology and general fault signatures are documented model
  inputs permitted by the rules.
- API credentials come only from environment variables and are redacted.
- Inference mode, config digest, model identifier and source coverage are logged
  for reproducibility.
- The technical report must distinguish the local prototype fallback from the
  competition LLM-assisted hybrid path.

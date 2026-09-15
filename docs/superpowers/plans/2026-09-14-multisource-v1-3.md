# 多源混合诊断模型 v1.3 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 修复 v1.2 的 P0 正确性问题，并完成能够由测试和公开/成都数据验证的 P1、P2 增强，形成可接 LLM 的 v1.3 事件与根因流水线。

**Architecture:** 保留七源预处理、鲁棒证据、事件切分、图融合和闭集分类边界；在证据层加入状态正常值、语义量程和冻结基线，在事件层收紧独立佐证，在 RCA 层统一角色权重，并增加证据缓存和完整诊断。内存与流式模式共享指标语义契约，正式数据仍采用受控内存流式扫描。

**Tech Stack:** Python 3.11+ 标准库、`unittest`、JSON/JSONL、现有 AIOps Challenge evaluator。

**Spec:** `docs/superpowers/specs/2026-09-14-multisource-v1-3-design.md`

## Global Constraints

- 不改变官方 CSV 输入和预测 JSONL Schema。
- 不读取正式数据 Ground Truth，不按 Case/城市/时间硬编码答案。
- 不使用固定 3/5 分钟过滤器；1 分钟 state/FRR 或极端量程故障仍可进入事件。
- `support` 不得开启事件，也不得挤掉 `trigger`；RCA 中必须降权。
- 正式扫描保持有界内存，缓存和日志不得包含 API Key 或原始 NetFlow 五元组。
- 每项生产行为先写测试并观察预期失败，再写最小实现。

---

### Task 1: 指标语义配置、状态正常值和量程信号

**Files:**
- Modify: `baseline/bian/preprocessing/metric_semantics.py`
- Modify: `baseline/bian/preprocessing/observations.py`
- Modify: `baseline/bian/config/metric_semantics.json`
- Modify: `baseline/bian/config/model_v1.json`
- Test: `tests/test_metric_semantics.py`
- Test: `tests/test_observations.py`

**Interfaces:**
- Consumes: `MetricSemantics.from_json(path)`、`CounterTransformer.transform(item)`。
- Produces: 带 `normal_value` 的 `NumericObservation`；无重复模式且吞吐为 support 的语义配置；`semantic_score` 字段合法的 `AnomalyEvidence`。

- [x] **Step 1: 写失败测试**

```python
def test_duplicate_metric_patterns_are_rejected(self):
    path.write_text('{"default":{},"rules":[{"pattern":"x.*"},{"pattern":"x.*"}]}')
    with self.assertRaisesRegex(ValueError, "duplicate metric pattern"):
        MetricSemantics.from_json(path)

def test_known_state_carries_normal_value_and_state_direction(self):
    result = CounterTransformer(self.semantics).transform(
        observation(0, "routing.bgp_peer_up", 0.0)
    )
    self.assertEqual((result[0].direction, result[0].normal_value), ("state", 1.0))

def test_throughput_is_support_only(self):
    result = CounterTransformer(self.semantics).transform(
        observation(0, "traffic.web.throughput_bps", 0.0)
    )
    self.assertEqual(result[0].event_role, "support")
```

- [x] **Step 2: 验证测试因当前缺陷失败**

Run: `python -m unittest tests.test_metric_semantics tests.test_observations -v`

Expected: FAIL，分别显示重复模式未拒绝、state 未携带正常值、吞吐仍为 trigger 或字段不存在。

- [x] **Step 3: 实现最小语义修复**

在 `MetricSemantics.from_json` 验证允许值和重复 pattern；`CounterTransformer` 对 state 设置 `direction="state"` 与 `normal_value`，Counter reset 设置 `normal_value=0.0`；删除重复吞吐规则并保留 support 版本。给 `NumericObservation.normal_value` 和 `AnomalyEvidence.semantic_score` 提供兼容默认值并执行有限值/区间校验。

- [x] **Step 4: 运行定向测试**

Run: `python -m unittest tests.test_metric_semantics tests.test_observations -v`

Expected: PASS。

### Task 2: 冻结基线、量程证据、角色安全保留与事件准入

**Files:**
- Modify: `baseline/bian/anomaly_detector/robust_detector.py`
- Modify: `baseline/bian/anomaly_detector/streaming_detector.py`
- Modify: `baseline/bian/config/model_v1.json`
- Test: `tests/test_robust_detector.py`
- Test: `tests/test_streaming_detector.py`

**Interfaces:**
- Consumes: 语义转换后的 `NumericObservation`。
- Produces: 离线/流式一致的 `AnomalyEvidence.semantic_score`；`_qualified_trigger_evidence` 只放行满足自身条件的证据；优先保留 trigger 的有界桶。

- [x] **Step 1: 写状态、保留和即时范围失败测试**

```python
def test_known_abnormal_state_emits_without_history(self):
    detector = OnlineRobustDetector(CONFIG)
    detector.add(point(0, 0.0, "routing.bgp_peer_up", direction="state", normal_value=1.0))
    self.assertEqual(detector.finalize()[0].direction, "state")

def test_support_cannot_displace_trigger_from_bounded_bucket(self):
    detector = OnlineRobustDetector({**CONFIG, "max_evidence_per_minute_source_node": 2})
    detector.add_evidence(support_25)
    detector.add_evidence(support_24)
    detector.add_evidence(trigger_8)
    self.assertTrue(any(point.event_role == "trigger" for point in detector.finalize()))

def test_immediate_state_does_not_admit_unrelated_gauge(self):
    events, _ = segment_evidence((state_point, unrelated_gauge), ...)
    self.assertFalse(any(point.metric == "node.disk_io_util" for point in events[0].evidence))
```

- [x] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest tests.test_robust_detector tests.test_streaming_detector -v`

Expected: FAIL，失败原因对应状态预热、support 截断和整分钟放行。

- [x] **Step 3: 实现状态直达与 trigger 优先保留**

为已知正常值 state 直接生成满量程受限证据；证据桶排序键首先比较 `event_role == "trigger"`，再比较分数。即时规则只加入 state/FRR 点自身。

- [x] **Step 4: 写持续性、因果族、语义量程和冻结基线失败测试**

```python
def test_cpu_and_load_are_one_causal_family(self):
    events, _ = segment_evidence((cpu_point, load_point), ...)
    self.assertEqual(events, [])

def test_extreme_semantic_position_can_open_one_minute_gauge_event(self):
    events, _ = segment_evidence((cpu_95_with_semantic_1,), ...)
    self.assertEqual(len(events), 1)

def test_frozen_baseline_detects_all_thirty_fault_minutes(self):
    evidence = _numeric_evidence(60_normal_plus_30_fault, freeze_config)
    self.assertEqual(len([p for p in evidence if p.value == 100.0]), 30)
```

- [x] **Step 5: 运行新增测试并确认 RED**

Run: `python -m unittest tests.test_robust_detector tests.test_streaming_detector -v`

Expected: FAIL，当前 CPU/load 相互佐证、无量程信号且基线会在长故障期间翻转。

- [x] **Step 6: 实现保守增强准入和冻结基线**

新增配置：`baseline_freeze_anomaly_points=30`、`min_persistent_trigger_minutes=2`、`corroboration_requires_cross_source_or_node=true`、`semantic_single_minute_threshold=0.9`、`metric_semantic_ranges`。同源同节点的 CPU/load、磁盘派生量和服务结果派生量折叠为同一因果族；持续性按窗口内达到阈值的异常分钟数计算；冻结期异常值不写入正常历史，超过冻结期后逐步适应。

- [x] **Step 7: 运行定向回归测试**

Run: `python -m unittest tests.test_robust_detector tests.test_streaming_detector -v`

Expected: PASS。

### Task 3: RCA 角色权重、NMS 质量和可观测候选作用域

**Files:**
- Modify: `baseline/bian/localization/graph_fusion.py`
- Modify: `baseline/bian/anomaly_detector/robust_detector.py`
- Modify: `baseline/bian/config/model_v1.json`
- Modify: `baseline/bian/run.py`
- Test: `tests/test_graph_fusion.py`
- Test: `tests/test_robust_detector.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: 带 `event_role`、`semantic_score` 的事件证据和公开拓扑。
- Produces: `RankingResult.scope`；候选记录中的 `trigger_anomaly_count`、`support_anomaly_count`；不受证据数量偏置的事件质量排序。

- [x] **Step 1: 写 RCA 与候选作用域失败测试**

```python
def test_high_score_support_does_not_outrank_direct_trigger(self):
    result = rank_candidates(event(trigger_8, support_25, support_25), NETWORK, TOPOLOGY, CONFIG)
    self.assertEqual(result.top5[0]["network_element_id"], trigger_8.node_id)

def test_explicit_cross_city_topology_edge_expands_candidate_scope(self):
    result = rank_candidates(event(local_point), two_city_network, cross_city_topology, CONFIG)
    self.assertIn("beida", result.scope["topology_expanded_cities"])
    self.assertGreater(result.scope["excluded_candidate_count"], 0)
```

- [x] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest tests.test_graph_fusion tests.test_pipeline -v`

Expected: FAIL，当前 support 等权且 `RankingResult` 不包含作用域。

- [x] **Step 3: 实现统一角色权重与候选作用域**

新增 `localization.support_evidence_weight=0.25`。严重度、持续度、来源多样性、直接性、关联支持和拓扑解释统一使用角色权重；候选作用域从直接、关联和显式跨城拓扑边构造，日志写入作用域及裁剪数。

- [x] **Step 4: 写并实现 NMS 质量测试**

先写两个相邻事件的测试：一个拥有大量同族派生点，另一个拥有更多有效异常分钟和独立来源；当前实现应错误保留前者。实现质量键为有效触发分钟、独立族/来源、最大语义位置、累计非饱和能量、峰值和确定性时间次序。

Run: `python -m unittest tests.test_robust_detector -v`

Expected: PASS。

### Task 4: 内存/流式语义一致性、实现指纹与切分前证据缓存

**Files:**
- Modify: `baseline/bian/preprocessing/metric_semantics.py`
- Modify: `baseline/bian/preprocessing/streaming.py`
- Modify: `baseline/bian/anomaly_detector/robust_detector.py`
- Modify: `baseline/bian/checkpoint.py`
- Modify: `baseline/bian/run.py`
- Test: `tests/test_streaming_pipeline.py`
- Test: `tests/test_checkpoint.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `transform_observations(iterable, semantics)`；`save_evidence_checkpoint(...)`、`load_evidence_checkpoint(...)`；覆盖检测源码的 `_pipeline_fingerprint(...)`；CLI `--evidence-cache`、`--reuse-evidence-cache`。

- [x] **Step 1: 写内存/流式一致性失败测试**

同一个小型七源 fixture 分别以 memory 和 streaming 运行，断言事件数、边界、根指标角色一致，并断言吞吐在两条路径均不能开启事件。

Run: `python -m unittest tests.test_streaming_pipeline -v`

Expected: FAIL，当前 memory 未执行 `CounterTransformer`。

- [x] **Step 2: 实现共享语义转换**

新增生成器 `transform_observations`，两条路径复用；memory 在 `detect_events` 前转换 numeric bundle，streaming 保持逐条转换和有界状态。

- [x] **Step 3: 写指纹和证据缓存失败测试**

```python
def test_pipeline_fingerprint_changes_with_detector_source(self):
    first = _pipeline_fingerprint(config, semantics, implementation_paths=(source_a,))
    source_a.write_text("changed")
    second = _pipeline_fingerprint(config, semantics, implementation_paths=(source_a,))
    self.assertNotEqual(first, second)

def test_evidence_checkpoint_round_trip_can_be_resegmented(self):
    save_evidence_checkpoint(points, path, observation_start=BASE, observation_end=END, metadata=meta)
    restored, bounds, restored_meta = load_evidence_checkpoint(path)
    self.assertEqual(restored, points)
```

- [x] **Step 4: 运行测试并确认 RED**

Run: `python -m unittest tests.test_checkpoint tests.test_pipeline -v`

Expected: FAIL，接口尚不存在或实现源码变化不影响指纹。

- [x] **Step 5: 实现指纹和独立证据缓存格式**

指纹按固定路径列表读取检测、流式、语义和事件切分实现字节；证据缓存保存 `format="bian-evidence"`、版本、观测边界、来源覆盖、流水线元数据和序列化证据。复用时校验版本和指纹，再调用 `segment_evidence`，不读取 CSV。

- [x] **Step 6: 运行定向测试**

Run: `python -m unittest tests.test_checkpoint tests.test_pipeline tests.test_streaming_pipeline -v`

Expected: PASS。

### Task 5: v1.3 日志、文档和全量验证

**Files:**
- Modify: `baseline/bian/config/model_v1.json`
- Modify: `baseline/bian/run.py`
- Modify: `README.md`
- Modify: `docs/deployment/server-inference.md`
- Test: `tests/test_pipeline.py`
- Outputs: `outputs/v1_3/sample/final/*`
- Outputs: `outputs/v1_3/chengdu/final/*`

**Interfaces:**
- Produces: `model_version=1.3`；包含分母/比例/候选作用域的推理日志；可复现的样例与成都审计结果。

- [x] **Step 1: 写日志字段失败测试**

运行 fixture 后断言：`total_minutes`、`nonzero_minute_ratio`、`trigger_nonzero_minute_ratio`、`event_covered_minutes`、`event_coverage_ratio` 存在且比例在 `[0,1]`；每个事件包含 `candidate_scope`。

Run: `python -m unittest tests.test_pipeline -v`

Expected: FAIL，v1.2 日志缺少这些字段。

- [x] **Step 2: 实现日志、版本和使用说明**

总分钟数来自 diagnostics 的完整分钟网格；事件覆盖按分钟集合去重；README 和服务器文档使用 v1.3 缓存名，说明旧缓存不可复用以及证据缓存的用途。

- [x] **Step 3: 运行完整单元测试**

Run: `python -m unittest discover -s tests -v`

Expected: 所有测试 PASS，无异常和资源警告。

- [x] **Step 4: 运行三个公开样例并评测**

```bash
python tools/run_sample_baseline.py \
  --output outputs/v1_3/sample/final/sample_v1_3_predictions.jsonl \
  --inference-log outputs/v1_3/sample/final/sample_v1_3_inference.json
python -m aiops_challenge_2026.evaluator \
  --ground-truth sample/ground_truth.jsonl \
  --predictions outputs/v1_3/sample/final/sample_v1_3_predictions.jsonl \
  --report outputs/v1_3/sample/final/sample_v1_3_report.json
```

Expected: 3 条 Schema 合法预测，三个公开故障均被覆盖；记录相对 v1.2 的分项分数。

- [x] **Step 5: 运行成都七源全量审计**

```bash
caffeinate -i /usr/bin/time -l python baseline/bian/run.py \
  --data-root data/stage1/regions/chengdu_20260819040000_20260902040000 \
  --ingestion-mode streaming \
  --allow-partial-input \
  --scratch-dir data/stage1/scratch/chengdu_v1_3 \
  --evidence-cache outputs/v1_3/chengdu/final/chengdu_v1_3_evidence.json \
  --event-cache outputs/v1_3/chengdu/final/chengdu_v1_3_events.json \
  --decision-backend local \
  --output outputs/v1_3/chengdu/final/chengdu_v1_3_predictions.jsonl \
  --inference-log outputs/v1_3/chengdu/final/chengdu_v1_3_inference.json
```

Expected: 七源行数守恒、无静默漏读、预测全部通过 Schema；汇总运行时间、峰值内存、事件数、时长、触发来源、类别和根因分布。

- [x] **Step 6: 检查差异和敏感信息**

Run: `git diff --check && rg -n "API_KEY=|incident-00|202608.*(root|answer)" baseline tests docs README.md`

Expected: `git diff --check` 无输出；没有密钥、正式答案或 Case 映射进入源码。

# 多源混合模型 v1.6 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 v1.5/DeepSeek 工作树升级为可审计的 v1.6，通过窗口级样本可信度确认 traffic 故障，并完善 RCA/LLM 证据、缓存隔离和历史生成物清理。

**Architecture:** 保留 `sample_count` 不进入时序主键的修复，在通用证据契约中增加 ratio 分子计数；事件切分阶段按稳定序列聚合连续 ratio/latency 窗口，只有满足持续性、累计样本和业务语义的窗口才能独立开事件。定位层区分 traffic 观测端和目标端支持，运行层输出资格路径、丢弃证据及候选模糊度审计。

**Tech Stack:** Python 3.11+、标准库 `unittest`、JSON/JSONL、现有 streaming/offline detector 和本地 evaluator。

**Spec:** `docs/superpowers/specs/2026-09-22-multisource-v1-6-design.md`

## Global Constraints

- 模型配置版本必须为 `1.6`，新输出统一写入 `outputs/v1_6/`。
- 不按约 292 个公开先验截断事件，不读取正式数据隐藏标签，不人工逐条标注。
- 七类数据读取和提交 schema 不得回归。
- `sample_count`、`numerator_count` 均不得进入 `series_key`。
- 没有官方映射时不得猜测 `target_domain` 对应的具体 `service-vm-*`。
- v1.5 旧 ratio evidence 不得静默作为 v1.6 evidence 复用。
- 删除只针对设计规范列出的可再生成路径；保留原始数据、样例、虚拟环境、源码和用户配置。

## Review Focus

- 计数器 reset 或采样间隔不是一分钟时，ratio 分子/分母必须仍使用真实计数增量而不是 rate。
- 同一时间重复 ratio 行必须按计数聚合，不能把中位 value 与第一条 sample_count 拼接。
- 三个低样本异常分钟不能通过 persistence 绕开累计样本门槛。
- traffic 目标端候选增强不能覆盖直接 node/routing/FRR 根因证据。
- 旧 checkpoint、缺失 numerator 的兼容输入和已失效缓存必须得到明确拒绝或保守降级。

---

### Task 1: Ratio 计数证据契约与 checkpoint v2

**Files:**
- Modify: `baseline/bian/preprocessing/observations.py`
- Modify: `baseline/bian/preprocessing/multisource.py`
- Modify: `baseline/bian/anomaly_detector/robust_detector.py`
- Modify: `baseline/bian/anomaly_detector/streaming_detector.py`
- Modify: `baseline/bian/checkpoint.py`
- Test: `tests/test_observations.py`
- Test: `tests/test_multisource.py`
- Test: `tests/test_checkpoint.py`
- Test: `tests/test_streaming_detector.py`

**Interfaces:**
- Produces: `NumericObservation.numerator_count: float | None`、`AnomalyEvidence.numerator_count: float | None`。
- Produces: checkpoint format v2，完整保存 `sample_count` 和 `numerator_count`。
- Consumes: DeepSeek 已加入的 `sample_count` 字段和稳定 `series_key`。

- [ ] **Step 1: 写入失败测试**

增加真实行为测试：

```python
def test_ratio_counts_do_not_change_series_identity(self):
    first = NumericObservation(..., sample_count=10, numerator_count=2)
    second = NumericObservation(..., sample_count=100, numerator_count=20)
    self.assertEqual(first.series_key, second.series_key)

def test_traffic_ratio_preserves_raw_numerator_and_denominator(self):
    self.assertEqual(ratio.sample_count, 20.0)
    self.assertEqual(ratio.numerator_count, 5.0)

def test_checkpoint_round_trip_preserves_ratio_counts(self):
    self.assertEqual(restored.numerator_count, 5.0)
```

- [ ] **Step 2: 验证 RED**

Run: `python3 -m unittest tests.test_observations tests.test_multisource tests.test_checkpoint tests.test_streaming_detector -v`

Expected: 新测试因 `numerator_count` 参数或字段不存在而失败，现有测试继续运行。

- [ ] **Step 3: 最小实现**

在两个冻结数据类中增加非负有限的 `numerator_count`，从 traffic counter delta 传入 ratio observation，并经 offline/streaming evidence 与 checkpoint 序列化。将 `FORMAT_VERSION` 和 `EVIDENCE_FORMAT_VERSION` 升为 2；旧格式报 `unsupported ... checkpoint version`，避免旧 ratio score 被误复用。

- [ ] **Step 4: 验证 GREEN**

Run: `python3 -m unittest tests.test_observations tests.test_multisource tests.test_checkpoint tests.test_streaming_detector -v`

Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add baseline/bian/preprocessing/observations.py baseline/bian/preprocessing/multisource.py baseline/bian/anomaly_detector/robust_detector.py baseline/bian/anomaly_detector/streaming_detector.py baseline/bian/checkpoint.py tests/test_observations.py tests/test_multisource.py tests/test_checkpoint.py tests/test_streaming_detector.py
git commit -m "feat: preserve traffic ratio count evidence"
```

### Task 2: Traffic ratio/latency 窗口资格判定

**Files:**
- Modify: `baseline/bian/anomaly_detector/robust_detector.py`
- Modify: `baseline/bian/config/model_v1.json`
- Test: `tests/test_robust_detector.py`
- Test: `tests/test_event_clustering.py`

**Interfaces:**
- Consumes: Task 1 的 `sample_count` 和 `numerator_count`。
- Produces: `_qualified_trigger_evidence_with_audit(...) -> (tuple[AnomalyEvidence, ...], tuple[dict[str, object], ...])`。
- Preserves: `_qualified_trigger_evidence(...) -> tuple[AnomalyEvidence, ...]` 兼容包装。

- [ ] **Step 1: 写入失败测试**

覆盖手工期望：三个每分钟 4 请求的异常点不能开事件；三个每分钟 12 请求且失败率严重的异常点可以开事件；累计 60 请求、18 失败、先验权重 30 的后验错误率为 0.2；纯 latency 四分钟不放行、五分钟放行；latency 与 outcome 同窗时三分钟放行。

- [ ] **Step 2: 验证 RED**

Run: `python3 -m unittest tests.test_robust_detector tests.test_event_clustering -v`

Expected: 至少低样本连续 ratio 被当前 DeepSeek persistence 错误放行，审计接口不存在。

- [ ] **Step 3: 最小实现**

删除通用 `source_persistence_min_minutes` traffic 放行。按稳定序列构建连续窗口：ratio 至少 3 分钟、累计样本至少 30、累计计数加 Beta 先验后跨越配置语义起点；latency 有 outcome 印证时 3 分钟，否则 5 分钟。保持 state、FRR、跨源 corroboration、非 traffic persistence 和 metric tier 逻辑不变。

- [ ] **Step 4: 验证 GREEN**

Run: `python3 -m unittest tests.test_robust_detector tests.test_event_clustering -v`

Expected: 全部通过，DeepSeek 的无条件 traffic persistence 测试改为窗口可信度测试。

- [ ] **Step 5: 提交**

```bash
git add baseline/bian/anomaly_detector/robust_detector.py baseline/bian/config/model_v1.json tests/test_robust_detector.py tests/test_event_clustering.py
git commit -m "feat: qualify traffic events with count windows"
```

### Task 3: RCA 观测端/目标端与 LLM 证据

**Files:**
- Modify: `baseline/bian/localization/graph_fusion.py`
- Test: `tests/test_graph_fusion.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: Task 1 的计数字段。
- Produces: ranking evidence JSON 中的 `sample_count`、`numerator_count`、`observer_support` 和 `target_support`。
- Preserves: 合法、唯一、确定性的 Top5 和直接基础设施证据优先级。

- [ ] **Step 1: 写入失败测试**

新增测试验证 ratio evidence 暴露分子/分母；traffic 目标候选拥有 `target_support` 而不是普通关系尾部；存在直接 CPU 根因时仍由 CPU 网元排名第一。

- [ ] **Step 2: 验证 RED**

Run: `python3 -m unittest tests.test_graph_fusion tests.test_pipeline -v`

Expected: 序列化字段和 target support 特征不存在。

- [ ] **Step 3: 最小实现**

为 traffic 相关证据生成明确的 observer/target 特征。目标服务候选不使用普通 related-only 的 0.8 症状惩罚，但仍低于具有直接 node/routing/FRR 证据的根因；不猜测具体 service VM。把计数和关系类型写入候选 evidence，供本地分类器和 LLM backend 使用。

- [ ] **Step 4: 验证 GREEN**

Run: `python3 -m unittest tests.test_graph_fusion tests.test_pipeline -v`

Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add baseline/bian/localization/graph_fusion.py tests/test_graph_fusion.py tests/test_pipeline.py
git commit -m "feat: expose traffic reliability to RCA"
```

### Task 4: 证据丢弃和候选模糊度审计

**Files:**
- Modify: `baseline/bian/anomaly_detector/streaming_detector.py`
- Modify: `baseline/bian/preprocessing/streaming.py`
- Modify: `baseline/bian/run.py`
- Test: `tests/test_streaming_detector.py`
- Test: `tests/test_streaming_pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: Task 2 的窗口规则和 Task 3 的候选特征。
- Produces: inference log 中的 `evidence_retention` 和 `ranking_margin_summary`，并按角色统计丢弃 evidence。

- [ ] **Step 1: 写入失败测试**

新增测试验证 bounded bucket 分别统计被丢弃的 trigger/support；推理日志包含 evidence retention、根因/分类 margin 和候选裁剪汇总。

- [ ] **Step 2: 验证 RED**

Run: `python3 -m unittest tests.test_streaming_detector tests.test_streaming_pipeline tests.test_pipeline -v`

Expected: 新审计字段不存在。

- [ ] **Step 3: 最小实现**

在 evidence bucket 删除时按角色计数；在运行报告中输出输入分钟、trigger 分钟、事件覆盖、低样本 driver、Top1/Top2 margin 和候选裁剪数量。审计只保存聚合值，避免重复写入百万条 evidence。

- [ ] **Step 4: 验证 GREEN**

Run: `python3 -m unittest tests.test_streaming_detector tests.test_streaming_pipeline tests.test_pipeline -v`

Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add baseline/bian/anomaly_detector/streaming_detector.py baseline/bian/preprocessing/streaming.py baseline/bian/run.py tests/test_streaming_detector.py tests/test_streaming_pipeline.py tests/test_pipeline.py
git commit -m "feat: audit evidence retention and ranking margins"
```

### Task 5: v1.6 集成验证和文档

**Files:**
- Modify: `baseline/bian/config/model_v1.json`
- Modify: `README.md`
- Modify: `docs/data/model-data-coordination.md`
- Create: `docs/evaluations/v1-5-baseline-summary.json`
- Test: `tests/test_public_samples.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `version = "1.6"`、可复现运行命令和机器可读 v1.5 基准摘要。
- Consumes: Tasks 1–4 的完整 pipeline。

- [ ] **Step 1: 写入失败测试**

增加测试确保旧 v1.5 checkpoint 被拒绝、v1.6 inference log 带 pipeline fingerprint、正式输出仍通过 schema。

- [ ] **Step 2: 验证 RED**

Run: `python3 -m unittest tests.test_public_samples tests.test_pipeline -v`

Expected: 至少版本/审计断言失败。

- [ ] **Step 3: 最小实现和文档**

配置版本改为 1.6；生成只含汇总统计的 v1.5 基准文件；更新 README 和数据协作文档，写明 traffic 域名到 VM 映射仍是外部数据需求，并提供 sample、traffic-only、单城七源、八城七源四级命令。

- [ ] **Step 4: 验证 GREEN 与数据烟测**

Run: `python3 -m unittest discover -s tests -v`

Run: `python3 baseline/bian/run.py --data-root sample --ingestion-mode streaming --decision-backend local --output outputs/v1_6/sample/predictions.jsonl --inference-log outputs/v1_6/sample/inference.json`

Run: `python3 -m aiops_challenge_2026.evaluator.cli --ground-truth sample/ground_truth.jsonl --predictions outputs/v1_6/sample/predictions.jsonl --output outputs/v1_6/sample/report.json`

Expected: 全套测试通过；三个公共样例保持 3 TP、0 FP、0 FN；输出通过 evaluator。若分数变化，必须由非样例特化的全局规则解释。

- [ ] **Step 5: 提交**

```bash
git add baseline/bian/config/model_v1.json README.md docs/data/model-data-coordination.md docs/evaluations tests/test_public_samples.py tests/test_pipeline.py
git commit -m "docs: publish v1.6 validation workflow"
```

### Task 6: 安全清理和最终验证

**Files:**
- Delete: 设计规范第 11 节列出的旧生成目录和缓存。
- Preserve: `outputs/README.md`、`outputs/v1_5/` 精简对照、`outputs/v1_6/`、`data/stage1/regions/`、`sample/`、`.venv/`、`.claude/`。

**Interfaces:**
- Consumes: Task 5 的 v1.5 精简摘要与 v1.6 验证输出。
- Produces: 无过期大型缓存、结构清晰的本地工作区。

- [ ] **Step 1: 清理前清单**

Run: `du -sh outputs/* data/stage1/scratch/* 2>/dev/null | sort -h`

Expected: 路径与设计规范清单一致，不包含原始数据目录。

- [ ] **Step 2: 删除明确目标**

删除 `outputs/legacy_unversioned`、`outputs/v1_1` 至 `v1_4`、`outputs/v1_5_fix*`、v1.5 大型中间缓存、空 scratch 子目录和项目内 `__pycache__`。不使用未解析变量、仓库根目录通配或 `git clean -fdx`。

- [ ] **Step 3: 清理后验证**

Run: `python3 -m unittest discover -s tests -v`

Run: `git status --short --ignored`

Expected: 测试全绿；原始数据、样例、虚拟环境、v1.5 精简基准和 v1.6 输出仍存在；旧生成目录不存在。

- [ ] **Step 4: 提交可跟踪变更**

如果清理只涉及被忽略文件则不创建空提交；如文档索引发生变化，精确添加后提交。


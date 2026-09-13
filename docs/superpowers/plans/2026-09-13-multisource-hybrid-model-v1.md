# 多源混合故障诊断模型第一版实施计划

> **供 Agent 执行：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐任务执行本计划。所有步骤使用复选框跟踪。

**目标：** 构建能利用七类观测数据、在本机输出合法预测、并预留 4×RTX 5090 与 OpenAI-compatible API 后端的第一版多源混合故障诊断模型。

**架构：** 七类 CSV 先转换成统一观测和有界分钟聚合，再由鲁棒滚动模型生成异常证据与事件；拓扑融合模型完成 Top5，闭集原型模型提供本机分类，LLM 后端可在服务器阶段重排和复核。生产推理不读取 Ground Truth，样例标签只在独立评测步骤使用。

**技术栈：** Python 3.10+ 标准库、`dataclasses`、`csv`、`statistics`、`urllib`、`unittest`；可选 `torch>=2.1`、`transformers>=4.40`、`accelerate`。

**设计文档：** `docs/superpowers/specs/2026-09-13-multisource-hybrid-model-design.md`

## 全局约束

- 所有生产代码改动必须先有失败测试，并完成红—绿—重构循环。
- 本机 `local` 后端不得依赖 Torch、外部 API 或网络。
- 推理模块不得打开、导入或扫描 `ground_truth.jsonl`。
- 不得出现 Case ID、公开样例答案、故障时间或网元答案映射。
- 所有输出必须通过 `aiops_challenge_2026.schema.validate_prediction`。
- Top5 必须是 1～5 连续排名、合法网元 ID 且不重复。
- NetFlow 解析不得把全部原始五元组保存在内存中。
- 非有限数值、畸形行和缺失数据不能使整次推理崩溃。
- 异常分数必须在 `[0, 25]` 内且有限。
- LLM 凭证只能从用户指定的环境变量读取，日志必须脱敏。

---

### 任务 1：统一观测数据契约与网元解析

**文件：**

- 新建：`baseline/bian/preprocessing/observations.py`
- 新建：`tests/test_observations.py`

**接口：**

- 产出：`NumericObservation`、`TextEvent`、`AnomalyEvidence`、`DetectedEvent`、`ParseStats`。
- 产出：`parse_time(value) -> datetime | None`。
- 产出：`normalize_node_id(raw, city, valid_roles) -> str | None`。
- 产出：`city_from_path(path, aliases) -> str | None`。
- 后续任务只通过这些类型交换观测、证据和事件。

- [ ] **步骤 1：编写网元、时间和数据类失败测试**

```python
class ObservationContractTests(unittest.TestCase):
    def test_normalize_router_and_reject_unknown_role(self):
        roles = ("br-1", "br-2", "service-vm-1")
        self.assertEqual(normalize_node_id("BR-1-ccf-aiops-西安", "xian", roles), "xian-br-1")
        self.assertIsNone(normalize_node_id("probe-vm", "xian", roles))

    def test_parse_time_returns_utc(self):
        parsed = parse_time("2026-07-28T12:39:34Z")
        self.assertEqual(parsed.isoformat(), "2026-07-28T12:39:34+00:00")

    def test_evidence_rejects_unbounded_score(self):
        with self.assertRaises(ValueError):
            AnomalyEvidence(
                timestamp=parse_time("2026-07-28T12:40:00Z"),
                source="node",
                node_id="xian-service-vm-1",
                related_node_ids=(),
                metric="node.cpu_usage",
                value=99.0,
                baseline=1.0,
                score=float("inf"),
                direction="high",
                dimensions=(),
                summary=None,
            )
```

- [ ] **步骤 2：运行测试，确认因模块不存在而失败**

运行：`python -m unittest tests.test_observations -v`

预期：`ModuleNotFoundError` 或缺少目标类型。

- [ ] **步骤 3：实现最小统一契约**

使用冻结数据类；在 `AnomalyEvidence.__post_init__` 中验证有限分数并截断由检测器负责；`parse_time` 把无时区输入按 UTC 解释；网元角色按长度降序匹配，避免 `service-vm-1` 被错误截断。

- [ ] **步骤 4：运行测试并确认通过**

运行：`python -m unittest tests.test_observations -v`

- [ ] **步骤 5：提交任务 1**

```bash
git add baseline/bian/preprocessing/observations.py tests/test_observations.py
git commit -m "feat: add canonical observation contracts"
```

---

### 任务 2：七类数据解析与流式聚合

**文件：**

- 新建：`baseline/bian/preprocessing/multisource.py`
- 新建：`tests/test_multisource.py`
- 新建：`tests/fixtures/multisource/processed/*.csv`

**接口：**

- 消费：任务 1 的统一数据类和解析函数。
- 产出：`load_observations(root, aliases, valid_roles) -> ObservationBundle`。
- `ObservationBundle` 包含 `numeric`、`text_events`、`stats` 和 `source_coverage`。
- 产出：`iter_source_files(root) -> Iterator[tuple[str, Path]]`。

- [ ] **步骤 1：为七类数据分别编写失败测试**

手写最小 CSV fixture，并断言：

```python
self.assertEqual(set(bundle.source_coverage), {
    "node", "interface", "routing", "scrape", "traffic", "netflow", "frr"
})
self.assertIn("interface.rx_drop_rate", metrics)
self.assertIn(("interface_id", "ens4"), interface_obs.dimensions)
self.assertIn("routing.bgp_peer_up", metrics)
self.assertIn(("peer", "fd00::1"), routing_obs.dimensions)
self.assertIn("traffic.web.error_ratio", metrics)
self.assertIn("netflow.bytes", metrics)
self.assertEqual(bundle.text_events[0].event_family, "bgp")
```

另测累计计数差分：值 `100, 130, 5` 产生增量 `30`，复位后的 `5` 产生 reset 标记而不是 `-125`。

- [ ] **步骤 2：运行测试并确认缺少解析器而失败**

运行：`python -m unittest tests.test_multisource -v`

- [ ] **步骤 3：实现文件分类、普通指标解析和维度保留**

实现节点、接口、路由和采集健康解析。数值转换只接受有限浮点数；解析失败记入 `ParseStats`。

- [ ] **步骤 4：运行局部测试，确认四类普通指标通过**

运行：`python -m unittest tests.test_multisource.MultiSourceTests.test_dense_metric_sources -v`

- [ ] **步骤 5：实现业务流派生特征和累计计数差分**

按完整业务流身份排序，生成 active、QPS、延迟、错误率、超时率、成功率、吞吐、丢包、重传和抖动信号；把源地域证据绑定到 `traffic-vm`，目标地域作为关联候选。

- [ ] **步骤 6：实现流式 NetFlow 分钟聚合**

使用字典保存分钟聚合器：

```python
key = (minute, node_id, interface_id, protocol)
aggregate.packets += packets
aggregate.bytes += bytes_value
aggregate.flow_records += flow_record_count
aggregate.unique_sources.add(src_addr)
aggregate.unique_destinations.add(dst_addr)
```

测试 fixture 很小；生产代码在完成单文件聚合后立即发出观测并释放行对象。

- [ ] **步骤 7：实现 FRR 日志事件族与分钟计数**

事件族仅使用通用关键词：`bgp`、`ospf`、`route`、`interface`、`config`、`process`、`other`。保存清洗后的 240 字符以内摘要。

- [ ] **步骤 8：运行全部解析测试并确认通过**

运行：`python -m unittest tests.test_multisource -v`

- [ ] **步骤 9：提交任务 2**

```bash
git add baseline/bian/preprocessing/multisource.py tests/test_multisource.py tests/fixtures/multisource
git commit -m "feat: parse all challenge observation sources"
```

---

### 任务 3：鲁棒异常评分和事件切分

**文件：**

- 新建：`baseline/bian/anomaly_detector/robust_detector.py`
- 新建：`tests/test_robust_detector.py`
- 修改：`baseline/bian/config/model_v1.json`

**接口：**

- 消费：`ObservationBundle.numeric` 和 `text_events`。
- 产出：`robust_score(value, history, config) -> tuple[baseline, score]`。
- 产出：`detect_events(bundle, config) -> list[DetectedEvent]`。
- 产出：`DetectionDiagnostics`，包含分钟能量、阈值和源覆盖。

- [ ] **步骤 1：编写零方差、方向和缺失值失败测试**

```python
baseline, score = robust_score(10.0, [0.0, 0.0, 0.0, 0.0], cfg)
self.assertTrue(math.isfinite(score))
self.assertLessEqual(score, 25.0)
self.assertGreater(score, cfg.evidence_threshold)
```

同时断言下降型指标只在下降时产生高分，双向指标两端均可异常。

- [ ] **步骤 2：运行评分测试并确认失败**

运行：`python -m unittest tests.test_robust_detector.RobustScoreTests -v`

- [ ] **步骤 3：实现滚动 Median/MAD、尺度下限和分数截断**

使用 `statistics.median`，尺度为：

```python
mad_scale = 1.4826 * median(abs(x - center) for x in history)
scale = max(mad_scale, absolute_floor, relative_floor * max(1.0, abs(center)))
score = min(max_score, abs(transformed_value - center) / scale)
```

- [ ] **步骤 4：运行评分测试并确认通过**

运行：`python -m unittest tests.test_robust_detector.RobustScoreTests -v`

- [ ] **步骤 5：编写事件迟滞和多个事件失败测试**

使用手写分钟序列，断言 5 分钟异常形成一个事件、1 分钟空隙被桥接、25 分钟安静期后的异常形成第二个事件、31 分钟事件在最低能量处拆分。

- [ ] **步骤 6：运行事件测试并确认失败**

运行：`python -m unittest tests.test_robust_detector.EventSegmentationTests -v`

- [ ] **步骤 7：实现源归一化、Top-K 能量和迟滞切分**

按分钟和 source 取最高 K 个证据分数，再对 source 能量取加权均值；高阈值开启、低阈值保持，结束时增加一个采样周期但不超过最后观测时间。

- [ ] **步骤 8：运行检测器测试并确认通过**

运行：`python -m unittest tests.test_robust_detector -v`

- [ ] **步骤 9：提交任务 3**

```bash
git add baseline/bian/anomaly_detector/robust_detector.py baseline/bian/config/model_v1.json tests/test_robust_detector.py
git commit -m "feat: add robust multi-source event detector"
```

---

### 任务 4：拓扑感知根因融合模型

**文件：**

- 新建：`baseline/bian/localization/graph_fusion.py`
- 新建：`tests/test_graph_fusion.py`
- 修改：`baseline/bian/config/model_v1.json`

**接口：**

- 消费：`DetectedEvent`、公开网元列表和参考拓扑。
- 产出：`rank_candidates(event, network_config, topology, config) -> RankingResult`。
- `RankingResult.top5` 直接符合预测 Schema，`candidates` 包含特征分量和证据。

- [ ] **步骤 1：编写合法性、时间领先和多源融合失败测试**

```python
result = rank_candidates(event, network, topology, config)
ids = [item["network_element_id"] for item in result.top5]
self.assertEqual(len(ids), 5)
self.assertEqual(len(ids), len(set(ids)))
self.assertEqual(ids[0], "xian-service-vm-1")
```

合成事件只描述通用 CPU 特征，测试名称和 fixture 不使用公开 Case ID。

- [ ] **步骤 2：运行测试并确认失败**

运行：`python -m unittest tests.test_graph_fusion -v`

- [ ] **步骤 3：实现候选特征提取和归一化**

计算 severity、persistence、precedence、source diversity、directness、recovery、scrape confidence 和 relational support；每一项归一化到 `[0,1]`。

- [ ] **步骤 4：实现区域内无向参考图和传播解释分数**

用 BFS 计算距离。直接证据不传播；关联业务流证据只向同地域的服务候选提供低权重支持；晚于上游直接证据的症状节点受到惩罚。

- [ ] **步骤 5：实现确定性 Top5 和完整审计分量**

并列时按 directness、precedence、node ID 排序，确保结果可复现。

- [ ] **步骤 6：运行根因测试并确认通过**

运行：`python -m unittest tests.test_graph_fusion -v`

- [ ] **步骤 7：提交任务 4**

```bash
git add baseline/bian/localization/graph_fusion.py baseline/bian/config/model_v1.json tests/test_graph_fusion.py
git commit -m "feat: rank root causes with topology fusion"
```

---

### 任务 5：闭集原型分类模型

**文件：**

- 新建：`baseline/bian/classification/prototype_model.py`
- 新建：`tests/test_prototype_model.py`
- 修改：`baseline/bian/config/model_v1.json`

**接口：**

- 消费：事件、候选排序和官方 taxonomy。
- 产出：`classify_event(event, ranking, taxonomy, config) -> ClassificationResult`。
- `ClassificationResult.category` 只包含合法 `major_category`、`sub_category`。

- [ ] **步骤 1：编写资源、路由和服务原型失败测试**

分别构造 CPU/负载、BGP down、Web error 证据，手写期望：

```python
self.assertEqual(cpu_result.category, {
    "major_category": "resource",
    "sub_category": "cpu_pressure",
})
```

另测所有 taxonomy 项均有原型，任何返回结果均为合法组合。

- [ ] **步骤 2：运行分类测试并确认失败**

运行：`python -m unittest tests.test_prototype_model -v`

- [ ] **步骤 3：实现事件信号向量和余弦相似度**

信号向量由 metric token、异常方向、根因角色、source、流类型和日志事件族生成；原型存放在配置文件，不包含样例标识。

- [ ] **步骤 4：实现大类先验和确定性并列处理**

根因角色仅作为弱先验。分类分数相同时按 taxonomy 原始顺序处理，输出置信度和 Top3 审计分数。

- [ ] **步骤 5：运行分类测试并确认通过**

运行：`python -m unittest tests.test_prototype_model -v`

- [ ] **步骤 6：提交任务 5**

```bash
git add baseline/bian/classification/prototype_model.py baseline/bian/config/model_v1.json tests/test_prototype_model.py
git commit -m "feat: add closed-set fault prototype model"
```

---

### 任务 6：LLM API 与四卡本地后端

**文件：**

- 新建：`baseline/bian/models/api_backend.py`
- 新建：`tests/test_api_backend.py`
- 修改：`baseline/bian/models/backend.py`
- 修改：`baseline/bian/prompts/7b_a_device_analysis.txt`
- 修改：`baseline/bian/prompts/7b_b_stage1.txt`
- 修改：`baseline/bian/prompts/7b_b_stage2.txt`
- 修改：`baseline/bian/prompts/classification.txt`

**接口：**

- API 和 Transformers 后端均实现既有
  `generate_json(*, role, prompt_name, payload, validator, max_new_tokens)` 契约。
- API 配置包含 `base_url`、`model`、`api_key_env`、`timeout` 和 `retries`。

- [ ] **步骤 1：用本地 HTTP 测试服务编写 API 失败测试**

测试服务返回合法 Chat Completions JSON，断言真实后端能够解析并验证 content；另测 HTTP 500 重试、无效 JSON 和日志脱敏。

- [ ] **步骤 2：运行 API 测试并确认失败**

运行：`python -m unittest tests.test_api_backend -v`

- [ ] **步骤 3：使用 `urllib.request` 实现最小 API 后端**

请求体使用 OpenAI-compatible `/chat/completions` 协议，Bearer token 只从指定环境变量读取。调用 `parse_and_validate` 复用现有严格 JSON 校验。

- [ ] **步骤 4：运行 API 测试并确认通过**

运行：`python -m unittest tests.test_api_backend -v`

- [ ] **步骤 5：编写本地后端设备映射行为测试**

把设备映射选择提取成不导入 Torch 的纯函数，断言 CUDA 多卡配置返回 `device_map="auto"`，CPU 配置不强制映射 GPU0。

- [ ] **步骤 6：修改 Transformers 后端并运行测试**

运行：`python -m unittest tests.test_api_backend tests.test_structured_output -v`

- [ ] **步骤 7：扩充 Prompt，使其明确识别七类证据、传播症状和分类边界**

Prompt 继续只要求 JSON，限制简短 reason，不加入任何样例答案。

- [ ] **步骤 8：提交任务 6**

```bash
git add baseline/bian/models baseline/bian/prompts tests/test_api_backend.py
git commit -m "feat: add scalable LLM decision backends"
```

---

### 任务 7：端到端编排、合法输出与推理日志

**文件：**

- 修改：`baseline/bian/run.py`
- 修改：`baseline/bian/preprocessing/evidence.py`
- 修改：`tools/run_sample_baseline.py`
- 新建：`tests/test_pipeline.py`
- 新建：`tests/test_output_writer.py`

**接口：**

- `run(data_root, output, model, use_llm, prediction_prefix, max_events,
  detector, decision_backend, api_base, api_key_env, config_path,
  inference_log)` 增加 detector、decision backend、API 和日志配置，同时保留旧参数兼容。
- 产出：预测 JSONL 和可选诊断 JSON 日志。

- [ ] **步骤 1：编写 local 流水线失败测试**

用合成七源目录调用真实 `run()`，断言：恰好一个预测、Top5 合法、分类合法、日志中七类 source coverage 均出现。

- [ ] **步骤 2：运行流水线测试并确认失败**

运行：`python -m unittest tests.test_pipeline -v`

- [ ] **步骤 3：将 robust detector、graph fusion 和 prototype classifier 接入 `run.py`**

保留 `--detector five-sigma` 作为回归路径；新默认值为 `robust` 和 `local`。`--use-llm` 映射到 `transformers`。

- [ ] **步骤 4：实现原子、Schema 校验后的 JSONL 输出**

先在目标目录创建命名临时文件，逐条调用 `validate_prediction`，全部成功后使用 `Path.replace()` 原子替换目标。

- [ ] **步骤 5：实现有界推理日志**

日志包含配置摘要、source coverage、坏行计数、分钟事件能量、事件边界、候选特征、分类 Top3 和后端信息，不包含 API Key 和完整 NetFlow 行。

- [ ] **步骤 6：运行流水线与输出测试并确认通过**

运行：`python -m unittest tests.test_pipeline tests.test_output_writer -v`

- [ ] **步骤 7：提交任务 7**

```bash
git add baseline/bian/run.py baseline/bian/preprocessing/evidence.py tools/run_sample_baseline.py tests/test_pipeline.py tests/test_output_writer.py
git commit -m "feat: integrate hybrid diagnosis pipeline"
```

---

### 任务 8：公开样例、官方评测和使用文档验收

**文件：**

- 修改：`README.md`
- 修改：`.gitignore`
- 新建：`tests/test_public_samples.py`

**接口：**

- 公开命令可以生成预测和推理日志。
- 测试通过临时目录保存生成物，不提交大文件。

- [ ] **步骤 1：编写公开样例集成失败测试**

逐个 Case 调用 local 模型，断言预测非空、结构合法、每个 Case 的 source coverage 与实际非空文件一致；测试推理函数无法访问 Case 根目录外的 Ground Truth。

- [ ] **步骤 2：运行公开样例测试并记录初始失败**

运行：`python -m unittest tests.test_public_samples -v`

- [ ] **步骤 3：修正仅由真实样例暴露的通用解析或边界问题**

每个修正先新增最小回归测试，再改生产代码。禁止根据 Case 名称、答案或 Ground Truth 调参。

- [ ] **步骤 4：运行三个样例 local 推理**

```bash
python tools/run_sample_baseline.py \
  --output outputs/hybrid_v1_predictions.jsonl \
  --decision-backend local \
  --inference-log outputs/hybrid_v1_inference.json
```

- [ ] **步骤 5：使用官方 evaluator 独立评测**

```bash
python -m aiops_challenge_2026.evaluator \
  --ground-truth sample/ground_truth.jsonl \
  --predictions outputs/hybrid_v1_predictions.jsonl \
  --report outputs/hybrid_v1_evaluator_report.json
```

- [ ] **步骤 6：更新 README**

写明本机 local、Transformers 多卡和 API 三种命令，解释 local 是开发回退路径、API Key 环境变量配置、推理日志字段和正式提交注意事项。

- [ ] **步骤 7：运行完整验证**

```bash
python -m unittest discover -s tests -v
python -m compileall aiops_challenge_2026 baseline tools
git diff --check
```

- [ ] **步骤 8：检查合规和仓库体积**

```bash
rg -n "incident-00|case_001.*xian|case_002.*guangzhou|case_003.*wuhan|API_KEY=" baseline tests README.md
git status --short
git diff --stat origin/main...HEAD
```

允许测试中出现通用 `case_001` 路径循环，但不得与答案网元或分类共同出现。

- [ ] **步骤 9：提交任务 8**

```bash
git add README.md .gitignore tests/test_public_samples.py
git commit -m "docs: document and verify hybrid model v1"
```

---

## 计划自检

- 七类数据接入由任务 2 覆盖，缺失/空文件行为由单元测试覆盖。
- AD、事件边界和低误报约束由任务 3、任务 7 和样例测试覆盖。
- Top5 合法性、排序和拓扑因果由任务 4、任务 7 覆盖。
- 28 类闭集输出由任务 5 覆盖。
- 本地、Transformers 多卡和 API 三种执行方式由任务 6、任务 7 覆盖。
- 正式输出、推理日志、Ground Truth 隔离和官方 evaluator 由任务 7、任务 8 覆盖。
- 全部新函数在其所属任务先写失败测试再实现。
- 未包含待定项、样例答案映射或依赖真实 API 的测试。

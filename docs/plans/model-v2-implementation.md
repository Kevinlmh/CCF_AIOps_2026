# 模型 v2.0 实施计划

## 1. 目标

依据 `docs/design/model-v2-architecture.md`，在 `main` 分支新增一条独立、可测试、可逐步替换旧基线的 v2.0 链路：

```text
七源 CSV → 分钟特征 → 张量窗口 → 自监督检测 → 全局事件解码
        → 事件子图 → 根因 Top5 → 故障分类 → 官方 JSONL
```

旧 `baseline/bian` 仅作为解析语义和结果对照，不作为 v2.0 检测兜底。

## 2. 实施原则

- 原始数据、特征缓存、模型权重和运行输出全部排除在 Git 之外。
- 每个阶段都有稳定输入输出契约和单元测试。
- 先建立正确的数据表示和全局事件机制，再优化模型精度。
- 本地可以用 CPU 和小样例验证；当前训练入口是单进程/单设备，服务器多卡训练需补 DDP 后再启用。
- 任何规则只产生特征或执行官方约束，不直接硬编码事件答案。

## 3. 里程碑

### M0：仓库与依赖

- 扩充 `.gitignore`。
- 增加 `v2-data`、`v2-model` 可选依赖。
- 建立 `aiops_v2` 包和统一配置加载。

验收：`data/`、模型权重、特征缓存不出现在 `git status`。

### M1：数据契约和稳定索引

- 定义节点、边、特征、分钟记录和特征清单数据类。
- 从官方配置构建固定 80 网元索引。
- 构建 topology、traffic、NetFlow 有向边索引。
- 生成可复现的 `manifest.json`、`entity_index.json` 和特征注册表。

验收：同一输入重复构建得到完全相同的索引与哈希。

### M2：流式分钟特征

- 复用经过验证的时间、节点和字段解析函数。
- 按特征语义执行 last/max/sum/mean 聚合。
- 正确处理 Counter 差分、复位、ratio 分母和缺失掩码。
- 将 FRR 日志映射为模板/严重度分钟特征。
- 输出按模态分片的 NumPy 内存映射特征存储。

验收：七源行数守恒；缺失与 0 可区分；全量构建内存有界。

### M3：窗口张量和标准化

- 实现 `[B,T,N,F_node]`、`[B,T,E,F_edge]`、`[B,T,N,F_log]`。
- 实现 mask、时间特征、滑窗与重叠区索引。
- 实现稳健 scaler，并只在 observed 位置拟合。

验收：小样例张量形状、mask、时间对齐和反序列化测试通过。

### M4：自监督检测模型

- 实现节点、有向边（traffic 与 NetFlow）和日志三分支编码器。
- 实现因果 TCN、门控多源融合和拓扑消息传递。
- 实现遮蔽重构、短期预测和一致性损失。
- 输出全网分钟概率、节点/边/因果族分数。

验收：CPU 小批次可以前向、反向和保存/加载；CUDA 单卡可用，多卡入口待实现。

### M5：全局事件解码

- 实现 1～30 分钟区间评分。
- 用动态规划选择全局不重叠事件。
- 将 20 分钟间隔作为软先验。
- 输出边界、峰值、置信度和诊断分解。

验收：合成序列中的短故障、长故障、相邻故障和恢复尾部测试通过。

### M6：事件子图、RCA 和分类

- 提取故障前、故障中、恢复后三段特征。
- 构建目标感知事件子图。
- 实现图排序头与官方 Top5 约束。
- 实现两级闭集分类头和合成扰动训练器。

验收：traffic 多源观测只产生一个事件；观察者不默认成为 Top1；类别均合法。

### M7：LLM 复核接口

- 生成有界结构化事件摘要。
- 接入本地 Transformers 和 OpenAI-compatible API。
- 校验事件意见、根因重排、分类和证据引用。
- 失败时回退本地模型。

验收：LLM 无法创建新事件、输出非法网元或非法类别。

### M8：端到端与全量验收

- 新增 `python -m aiops_v2.run` 入口。
- 写入预测、推理日志、数据审计、配置和模型哈希。
- 跑三个公开样例、合成测试和八城全量测试。
- 完成无 traffic、无日志、无图、无 LLM 的消融。

验收：官方 schema/evaluator 通过，运行可复现，事件全局不重叠。

## 4. 第一轮直接实现范围

本轮先完成可以独立验证架构正确性的纵向切片：

1. M0 仓库忽略规则与依赖；
2. M1 数据契约和稳定索引；
3. M2 小样例可用的分钟级特征构建；
4. M3 张量窗口；
5. M4 模型骨架与训练损失；
6. M5 全局事件解码；
7. 最小端到端 CLI 和测试。

随后在同一方案内继续实现 M6～M8，不再重新设计数据结构。

## 5. 预期命令

```bash
# 训练与推理依赖
python3 -m pip install -e '.[v2-model]'

# 仅在需要调用本地 Transformers LLM 时安装
python3 -m pip install -e '.[llm]'

# 构建分钟特征
python3 -m aiops_v2.run build-features \
  --data-root sample \
  --store data/feature_store/v2/sample

# 训练自监督模型
python3 -m aiops_v2.run train \
  --store data/feature_store/v2/sample \
  --checkpoint checkpoints/v2/model.pt

# 推理并写官方格式
python3 -m aiops_v2.run predict \
  --store data/feature_store/v2/sample \
  --checkpoint checkpoints/v2/model.pt \
  --output outputs/v2/sample_predictions.jsonl

# 单命令串起全流程
python3 -m aiops_v2.run all \
  --data-root sample \
  --store data/feature_store/v2/sample \
  --checkpoint checkpoints/v2/model.pt \
  --output outputs/v2/sample_predictions.jsonl \
  --inference-log outputs/v2/sample_inference.json

# 可选：经 OpenAI-compatible 服务复核低置信事件的 Top5/类别
export AIOPS_LLM_API_KEY="<服务密钥>"
python3 -m aiops_v2.run predict \
  --store data/feature_store/v2/sample \
  --checkpoint checkpoints/v2/model.pt \
  --output outputs/v2/sample_predictions_llm.jsonl \
  --inference-log outputs/v2/sample_inference_llm.json \
  --llm-backend openai-compatible \
  --llm-model "<模型名>" \
  --llm-base-url "http://<服务地址>/v1"
```

服务器端可以沿用相同的数据和推理命令；当前训练只会使用 `--device cuda` 指定的一张 GPU，四卡训练要等 DDP 入口实现后再开展。

## 6. 当前落地状态（2026-09-23）

| 里程碑 | 代码状态 | 仍需注意 |
|---|---|---|
| M0–M3 | 已有依赖、数据契约、七源流式解析、分钟特征存储与张量窗口 | 全量资源消耗和字段覆盖需要在服务器数据上验收 |
| M4 | 已有分模态因果 TCN、遮蔽重构/预测、时间上下文、traffic 目标侧图消息与跨源/拓扑一致性损失 | 目前是单进程训练；尚无 DDP/四卡训练入口 |
| M5 | 已有全局动态规划事件解码 | 概率校准仍依赖无标签低能量时间段，需看全量审计，不以 292 调参 |
| M6 | 已有故障前/早期/持续/恢复分段的事件子图证据排序、目标服务边归因和闭集语义分类 | RCA 与分类目前不是训练出的图排序头/分类头；没有测试标签时不伪称监督模型 |
| M7 | 已接 OpenAI-compatible 与本地 Transformers 的受约束复核接口，错误时熔断并回退本地输出 | 未接真实模型服务；事件边界与事件数量始终不受 LLM 改写 |
| M8 | `build-features/train/predict/all` CLI、官方 JSONL 校验、预测审计日志和清单/权重 SHA-256 | 尚未完成八城全量、官方 evaluator、公开样例回归和消融验收 |

本轮续作针对短故障序列中的校准污染和稀疏异常池化进行了代码修正，并补上了事件子图、受约束 LLM 接口和数据审计输出。由于本轮不执行测试命令，以上新增/修改代码尚未完成回归验证；不得据此宣称预测质量已经提升或 v2.0 已通过全量验收。

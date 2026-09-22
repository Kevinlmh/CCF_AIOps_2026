# 多源混合模型 v1.6 设计规范

## 1. 目标

v1.6 面向八城七源正式数据修复 v1.5 暴露的系统性问题，而不是围绕三个公共样例调参。版本目标是：

1. 保留 `sample_count` 不参与时序主键的正确修复，恢复稳定的 traffic ratio 基线。
2. 用带样本可信度的窗口级判据替换“traffic 连续三分钟即放行”。
3. 将请求规模、事件计数、资格原因和观测方向贯穿检测、缓存、定位及 LLM 输入。
4. 保持七类数据完整读取，避免通过目标事件数量或人工标签硬裁剪候选。
5. 增强全量任务的可审计性、缓存隔离和版本可复现性。
6. 清理已经失效且可重新生成的历史输出，同时保留原始数据、源码、测试和必要的版本对照材料。

## 2. 设计原则

- 不把公开的约 292 个故障数量当作预测截断目标。
- 不进行人工逐条标注或针对未知正式答案拟合。
- 公共样例用于接口和基本回归，不作为唯一优化目标。
- 事件必须由数据质量、持续性、语义严重度或跨源证据支持。
- 低流量业务通过跨分钟累计样本获得可信度，而不是使用统一的单分钟硬阈值。
- 不在缺少官方映射时臆测 `target_domain` 对应的具体 `service-vm-*`。
- 对缓存和模型行为的实质变化必须升级版本并拒绝静默复用旧结果。

## 3. 当前问题与取舍

### 3.1 保留的 DeepSeek 修复

请求样本量是观测属性，不是序列身份。`window_requests` 放在 `dimensions` 中会使每分钟请求量变化都产生一个新序列，从而破坏滚动基线、预热和持续性判断。

v1.6 保留以下行为：

- `NumericObservation.sample_count` 和 `AnomalyEvidence.sample_count`；
- traffic ratio 的 `dimensions` 不包含请求数；
- streaming、offline 和 checkpoint 全链路传递样本量；
- 缺少样本量的旧 ratio 证据不能走单分钟语义极值通道。

### 3.2 替换的 DeepSeek 规则

删除 `source_persistence_min_minutes.traffic = 3` 的无条件放行。八城原始 traffic 对照显示，该规则会把 traffic-only 事件从 364 增加到 555，把 trigger 活跃分钟从 673 增加到 4050，并使低于 30 个请求的驱动事件从 2 增加到 130。

这一规则只证明了“连续三分钟时程序列可以放行”，没有证明这些连续点具备足够业务样本或故障语义。

## 4. 数据与证据契约

### 4.1 数值观测

`NumericObservation` 新增并保留以下可选字段：

- `sample_count: float | None`：派生比例背后的分母，例如请求总数；
- `numerator_count: float | None`：派生比例背后的分子，例如成功数或失败数。

两者均为非负有限数，不进入 `series_key`。普通 gauge、state、routing、node、interface、netflow 和 scrape 指标可保持为 `None`。

### 4.2 异常证据

`AnomalyEvidence` 继承 `sample_count` 和 `numerator_count`。原始 evidence 不写入事件资格结果，因为资格判定发生在事件切分阶段；切分阶段另行生成资格审计记录：

```text
evidence identity
qualification reason
window start/end
window sample count
window numerator count
aggregated posterior ratio
```

资格原因限定为：

- `state`
- `frr`
- `corroboration`
- `persistence`
- `semantic_extreme`
- `traffic_ratio_window`
- `traffic_latency_window`

### 4.3 重复时间点

同一序列、同一时间点存在重复观测时：

- 普通 gauge 继续使用中位数；
- ratio 不直接复制第一条的样本量；
- ratio 按分子和分母求和后重新计算带先验的比例；
- 无法获得分子时，使用样本量加权平均并在审计中记录降级处理。

## 5. Traffic 窗口级确认

### 5.1 Ratio 窗口

成功率和错误率必须满足全部条件，才能通过 `traffic_ratio_window` 独立成为 trigger：

1. 同一稳定序列存在至少 3 个连续异常分钟；
2. 窗口累计 `sample_count >= 30`；
3. 按累计分子、分母和配置的 Beta 先验重新计算窗口后验均值；
4. 聚合成功率不高于 `low_start`，或聚合错误率不低于 `high_start`；
5. 每个参与分钟已经通过原有 robust score evidence threshold。

单分钟 ratio 只有同时满足以下条件才能通过 `semantic_extreme`：

- `sample_count >= 30`；
- `semantic_score >= 0.9`。

该设计允许低流量 auth 服务跨分钟累计样本，不允许三个极小样本分钟绕过可信度检查。

### 5.2 Latency 窗口

latency evidence 采用两条路径：

- 与同一服务的 outcome family 同窗相互印证时，连续至少 3 分钟；
- 没有 outcome 印证时，连续至少 5 分钟。

latency 的 counter sum/count rate 继续作为 support，只有已定义的 mean、p95 等延迟指标能够成为 latency trigger。暂不使用从全量无标签数据反推的固定绝对秒数阈值。

### 5.3 其他 traffic 指标

- throughput、observed QPS、原始 success/error rate 和 histogram sum/count 保持 support；
- counter reset 只作为上下文，不开事件；
- elephant flow 不因流量大自动成为故障；
- 不同服务、城市、域名和协议不能相互拼接持续窗口。

## 6. 事件生成与其他数据源

- node、routing、interface、scrape、netflow 和 FRR 保持现有语义规则，避免在没有新证据时大范围调参。
- state 和 FRR 仍可即时放行自身证据，但不能“祝福”同一分钟的无关 trigger。
- 切分后的子事件必须重新验证 trigger、持续性和 confidence。
- 城市隔离继续作用于 trigger 能量和事件切分；显式拓扑关系只用于定位候选扩展。
- 继续保留 CPU 量程和持续性层级、disk semantic range、零值 support 抑制和恢复尾部裁剪。
- 对 30 分钟附近的长故障新增审计测试，暂不在无真实标签的情况下整体替换 robust score 为变化点模型。

## 7. 根因定位与 LLM 输入

### 7.1 Traffic 双向语义

traffic evidence 同时表达：

- 观测端：来源城市的 `traffic-vm`；
- 目标端：`target_region` 内的服务候选；
- 业务标识：`flow_type`、`target_domain`、protocol；
- 可信度：样本量、分子、窗口后验比例、持续时间和资格原因。

在没有官方域名到 VM 编号映射时，三个 `service-vm-*` 均保留为候选，不猜测精确编号。

### 7.2 排名约束

- 不将 `traffic-vm` 一律排除或硬降为末位；
- 不把目标服务候选视为普通的弱拓扑尾部；
- 在候选特征中区分 `observer_support` 与 `target_support`；
- 直接 node/routing/FRR 证据仍优先于纯 traffic 症状；
- 输出候选分差和被候选城市过滤掉的数量；
- LLM 只能在合法候选集内重排，不能创造不存在的网元 ID。

### 7.3 推理上下文

传给本地分类器和 LLM 的强证据至少包含：

- value、baseline、robust score、semantic score；
- sample_count、numerator_count；
- qualification reason 和聚合窗口摘要；
- source/target city、target domain、flow type；
- event role、direction、持续分钟数；
- direct、observer、target、topology 四类关系。

## 8. 审计与运行报告

v1.6 推理日志新增：

- `model_version`、pipeline fingerprint、配置摘要和 Git commit（可获得时）；
- 各资格原因的 evidence 数和事件数；
- ratio 窗口累计请求数、分子和后验比例分布；
- 低样本 traffic 驱动事件数；
- trigger/support 各自的 retained 和 dropped 数；
- 每类驱动指标、数据源组合、事件时长和城市分布；
- 根因及分类 Top1/Top2 margin；
- 纯单源、跨源、跨城市关系事件数；
- 输入总分钟、trigger 非零分钟和事件覆盖分钟。

日志不得输出 API key、完整 prompt 密钥或其他环境机密。

## 9. 版本与缓存

- 模型配置版本改为 `1.6`。
- event/evidence checkpoint 格式升级为 v2。
- v1.6 不直接复用 v1.5 evidence，因为旧 ratio 分数来自被请求量拆碎的序列。
- checkpoint metadata 必须包含模型版本、pipeline fingerprint 和输入边界。
- 所有新结果写入 `outputs/v1_6/`。

## 10. 测试与验收

### 10.1 单元和集成测试

覆盖以下场景：

1. 请求量变化不改变 ratio 的 `series_key`。
2. `sample_count`、`numerator_count` 经过 streaming、offline 和 checkpoint 不丢失。
3. 三个低样本异常分钟不能独立开事件。
4. 低流量服务跨分钟累计达到 30 个请求后可以由真实严重比例放行。
5. 高样本单分钟只有语义极端时可以放行。
6. latency 三分钟有 outcome 印证时放行，纯 latency 需五分钟。
7. 不同域名、城市、服务和协议不能拼接窗口。
8. ratio 重复时间点按计数聚合。
9. 资格原因进入审计和 LLM 上下文。
10. 30 分钟异常不会无声产生 support-only 子事件或越界输出。

### 10.2 数据级验收

- 三个公共样例继续满足合法格式并保持现有 TP，不以其分数单独决定规则。
- 八城 traffic 原始数据对比 v1.5 保守规则和 DeepSeek 规则，报告事件数变化但不设置目标总数。
- 低样本量 driver 不得通过 persistence 绕过窗口累计样本要求。
- 八城七源全量运行必须保持输入行守恒、七源齐全、无 support-only 事件和无非拓扑跨城粘连。
- 输出必须通过官方 evaluator 和 schema 校验。

## 11. 文件清理策略

### 11.1 删除

完成 v1.5 精简摘要后删除以下可再生成内容：

- `outputs/legacy_unversioned/`
- `outputs/v1_1/`、`outputs/v1_2/`、`outputs/v1_3/`、`outputs/v1_4/`
- `outputs/v1_5_fix/`、`outputs/v1_5_fix2/`
- v1.5 的大型 evidence、event、inference、resegmentation 和临时 replay 文件
- 空的 `data/stage1/scratch/*`
- 项目内所有 `__pycache__`

### 11.2 保留

- `data/stage1/regions/` 正式原始数据；
- `sample/` 官方样例和 ground truth；
- `.venv/` 当前运行环境；
- 源码、测试、需求、设计、实施计划和数据协调文档；
- v1.5 的精简基准摘要、全量日志、最终 predictions、evidence audit 和 traffic 对照报告；
- 用途不明确的用户配置目录，例如 `.claude/`。

删除前通过明确路径清单和磁盘占用复核目标，不使用面向仓库根目录的递归通配删除。

## 12. 非目标

v1.6 不包括：

- 训练端到端 Transformer 或 GNN；
- 下载、微调或部署正式 LLM 权重；
- 根据隐藏答案进行人工事件标注；
- 根据 292 个公开先验强制裁剪输出；
- 在没有映射证据时指定域名对应的 service-vm 编号；
- 用全量无标签分布一次性替换所有指标阈值。


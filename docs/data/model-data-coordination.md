# 模型侧与数据侧协作契约

## 1. 共同目标与边界

数据侧负责提供可复核的数据事实、字段语义、实体关系和数据质量报告；模型侧负责把这些事实固化为读取规则、指标语义配置、特征、测试和推理日志。双方共用 `baseline/bian/preprocessing/` 的读取规则，禁止各自维护另一套节点映射、时间处理或空值解释。

本项目当前处理的第一批正式数据没有可用 Ground Truth。不得依据波形、模型预测或个人判断，人工填写故障时间窗、根因设备和故障类别，并将其当作真实标签训练、调参或汇报成绩。

### 允许使用的监督信息

- 官方公开的三个样例及其 `sample/ground_truth.jsonl`；
- 官方后续明确发布的训练标签；
- 官方榜单或评测系统返回的合法分项反馈；
- 赛事规则明确允许使用的公开配置、网络拓扑和故障分类表。

### 允许开展但不属于“人工标注”的工作

- 字段含义、单位、量程和采样方式核验；
- 缺失、重复、乱序、Counter reset 和周期性统计；
- 对模型候选事件进行“待核验”“周期波动”“采集异常”等分析性备注；
- 对官方有标签样例制作证据卡；
- 对无标签正式数据进行无监督聚类、分布分析和模型间一致性比较。

分析性备注不能改名为 Ground Truth，也不能直接作为监督标签进入训练集。第一批正式数据中的 167 个成都候选事件、其中 132 个 `traffic-vm` Top1，均属于模型输出和审计对象，不代表官方真实故障数。

## 2. 数据侧必须交付的结果

数据侧优先交付事实表和可复现脚本，不直接修改模型阈值。建议将结果放在 `docs/data/reports/`，统计脚本放在 `tools/data_audit/`；大体积中间数据继续放在被 Git 忽略的 `data/` 或 `outputs/`。

### 2.1 八城七源文件清单

按“城市 × 来源 × 文件”提供以下字段：

| 字段 | 含义 |
|---|---|
| city | 官方城市名 |
| source | node、interface、routing、scrape、traffic、netflow、frr |
| relative_path | 相对数据根目录的路径 |
| size_bytes | 文件字节数 |
| rows_read | CSV 数据行数，不含表头 |
| start_time/end_time | 最早和最晚 UTC 时间 |
| sampling_interval | 主要采样间隔及异常间隔数量 |
| duplicate_rows | 重复行数 |
| missing_minutes | 按应有采样频率计算的缺失分钟数 |
| out_of_order_rows | 时间乱序行数及最大乱序幅度 |
| header_hash | 表头版本标识 |

清单必须能与推理日志中的 `files`、`rows_read`、`time_ranges` 和 `rows_conserved` 对账。发现差异时先定位读取或统计口径，不得通过删行使数字一致。

### 2.2 字段与指标语义表

每个数值字段至少提供：

| 字段 | 必填说明 |
|---|---|
| source/metric | 原始字段及统一指标名 |
| description | 业务含义 |
| unit | 百分数、0～1 比例、字节/秒、累计次数等 |
| kind | Gauge、Counter、State、Event、Context |
| expected_range | 合法物理量程，不等同于故障阈值 |
| normal_value | 只有业务定义固定正常值的 State 才填写 |
| anomaly_direction | high、low、both、state |
| event_role | trigger、support、context |
| reset_rule | Counter 的重启、回绕和重置规则 |
| missing_meaning | 缺失表示无流量、采集失败、未配置或未知 |
| dimensions | 定义一条时间序列所需的 label/接口/协议 |
| evidence | 文档、代码或数据统计依据 |

该表用于模型侧复核 `baseline/bian/config/metric_semantics.json` 和 `baseline/bian/config/model_v1.json`。数据侧不要在两个配置文件中分别复制规则；模型侧修改配置时必须补回归测试。

### 2.3 实体与服务映射表

汇总以下标识的全部取值及映射关系：

- `node`、`node_key`、`hostname`、`target_id`、`region`；
- 官方 80 个候选网络元素；
- traffic 的 source/target、域名与 DNS/Web/Auth 服务；
- NetFlow 采集节点、接口 ID、方向和真实流量路径；
- FRR hostname 与路由设备；
- scrape target 与被采集设备；
- 接口 ID 与公开拓扑链路。

无法映射到官方候选元素的值必须单独列出，说明其属于探针、观测节点、容器实例、未知节点还是脏数据。不得凭名称相似度人工指定根因。

### 2.4 分布、周期性和跨源对齐报告

对每个“城市 × 节点角色 × 指标”至少统计：

- 非空数量、零值比例、唯一值数量；
- min、P01、P05、P25、P50、P75、P95、P99、max；
- 每小时和每天的周期性；
- 连续高值/低值段长度分布；
- Counter reset 次数；
- 缺失段和采集失败段；
- 跨城市分布差异；
- 与其他来源的时间领先/滞后分布。

这类统计用于判断指标量程、噪声和采样行为，不直接产生故障标签。例如 `observed_qps` 的下降可能只是负载变化，因此当前仅作为 support；如果数据事实证明它在固定业务条件下可独立表示服务中断，再由模型侧通过配置、测试和官方样例回归评估是否升级为 trigger。

### 2.5 官方样例证据卡

只对官方已有 Ground Truth 的三个公开 Case 制作证据卡，每张包含：

- 官方故障时间窗、根因和类别；
- 最早直接证据；
- 根因设备的持续指标变化；
- 下游传播症状；
- 恢复证据；
- 反证及易混淆类别的排除理由；
- 对应原始文件、字段和时间戳。

正式第一批无标签数据只能制作“候选事件审计卡”，标题和字段必须明确写成 candidate，不得填写 `ground_truth`、`true_root_cause` 或 `true_category`。

## 3. 模型侧向数据侧提供的结果

每次正式流式运行的推理日志提供：

- `files`、`rows_read`、`valid_rows`、`filtered_rows`、`invalid_rows`；
- `emitted_observations`、`unknown_nodes`、`time_ranges`、`rows_conserved`；
- `observations_evaluated`、`series_state_count`、按 trigger/support 拆分的 `dropped_evidence`；
- `total_minutes`、非零分钟比例和事件覆盖比例；
- 每个事件的 Top5、候选作用域、trigger/support 数量、分类 Top3 和原型信号；
- 本地规则模型与 LLM 结果的差异。

数据侧发现统计不一致时，按以下顺序定位：

1. 文件清单；
2. 行数守恒；
3. 时间解析和采样间隔；
4. 节点及服务映射；
5. Counter/State/Gauge 转换；
6. trigger/support/context 角色；
7. 异常打分和事件切分。

在前六项尚未确认前，不建议直接调整异常阈值。

## 4. 当前优先需要数据侧确认的问题

1. `cpu_usage` 是 0～100 百分数还是 0～1 比例；当前数据正常值接近 0，但峰值可接近 100。
2. `disk_io_util` 的单位、合法量程和采集聚合方式。
3. `memory_available_ratio` 是否已标准化为 0～1，以及容器缓存是否计入 available。
4. `ipv6_route_change_total`、`ipv6_default_route_changed_total` 和 `carrier_changes` 是否始终为累计 Counter。
5. `ospf6_neighbor_state_code` 的完整状态枚举及哪些状态代表异常。
6. `routing.*enabled` 和 `routing.*route_exists` 的 0 是否可能表示“未配置但正常”；当前模型不再假设其固定正常值为 1。
7. traffic 的 `*_total` 是否每分钟采样、是否跨进程重置，以及各 ratio 的分子分母。
8. `observed_qps`、throughput 和 request count 的业务区别及其正常周期性。
9. `scrape_up=0` 时，同节点其他来源的数据是否仍可信。
10. FRR 中 `sendmsg failed`、`Could not send entire message`、`Operation not permitted` 是否为环境噪声。
11. DNS/Web/Auth 各域名对应哪个 `service-vm-*`，能否把关系证据从三个模糊候选收敛到具体服务节点；v1.6 在映射确认前只区分观测端和目标端，不猜测 VM 编号。
12. NetFlow 的 `if_role=\N` 是否存在外部接口映射表。
13. 第一批数据是否允许多个故障时间重叠，以及同一故障是否可能跨城市传播；只接受官方规则或官方说明作为结论依据。
14. 每个 traffic ratio 的请求数、分子计数和窗口时长是否严格对应同一 Counter 序列；v1.6 按连续窗口累计计数，不再把请求量放进时序主键。
15. `error_total` 是否可能包含重叠错误类别而大于 `requests_total`；若可能，需提供字段口径，避免把它直接解释为概率。

## 5. 八城全量预测前的验收清单

模型可以直接对 `data/stage1/regions` 运行八城全量预测。开始前应确认：

- 八城市 × 七来源文件全部存在且没有重复文件；
- 数据根目录结构能通过严格清单检查；
- 服务器本地 NVMe 有足够空间保存 scratch、证据缓存和事件缓存；
- Git 当前版本、模型版本和配置指纹已记录；
- 不添加 `--allow-partial-input`，让缺失城市或来源直接报错；
- 输出、证据缓存、事件缓存和日志使用唯一版本名，避免覆盖；
- API Key 只通过环境变量提供；
- Ground Truth 文件不出现在正式推理命令中。

CPU 本地基准中，成都两周七源约处理 24,512,574 条规范化观测，耗时约 10 分钟，峰值常驻内存约 410 MiB。八城全量可粗略按 70～120 分钟规划；实际时间主要受 CPU、CSV 解析和 NVMe 吞吐影响，4 张 GPU 不会显著加速该阶段。

推荐的八城纯本地基准命令：

```bash
/usr/bin/time -v python baseline/bian/run.py \
  --data-root data/stage1/regions \
  --ingestion-mode streaming \
  --scratch-dir /fast-nvme/aiops-scratch/v1_6_all_cities \
  --evidence-cache outputs/v1_6/all_cities/stage1_evidence.json \
  --event-cache outputs/v1_6/all_cities/stage1_events.json \
  --decision-backend local \
  --output outputs/v1_6/all_cities/stage1_local_predictions.jsonl \
  --inference-log outputs/v1_6/all_cities/stage1_local_inference.json
```

建议在 Linux 服务器的 `tmux`/`systemd` 会话中运行。若仍在 macOS 本机执行，把 `/usr/bin/time -v` 改为 `caffeinate -i /usr/bin/time -l`，并把 `/fast-nvme/aiops-scratch/` 换成本机 `data/stage1/scratch/`。

## 6. 服务器与 LLM 阶段协作流程

全量流程采用“CPU 事件生成”和“GPU 事件级复核”两阶段，避免每次修改 Prompt 或 LLM 参数都重新扫描原始数据。

### 阶段 A：CPU 流式检测

产物包括：

- `stage1_evidence.json`：切分前证据，可用于重新调整事件门控和切分；
- `stage1_events.json`：已切分事件，可直接用于 RCA、分类和 LLM；
- `stage1_local_predictions.jsonl`：确定性本地模型基准；
- `stage1_local_inference.json`：数据质量和候选审计日志；
- 命令、Git commit、配置指纹、运行时间和峰值内存记录。

### 阶段 B：四卡 LLM 推理

LLM 服务读取事件级摘要，不直接读取全部 CSV。模型侧复用同一事件缓存：

```bash
export AIOPS_LLM_API_BASE='http://127.0.0.1:8000/v1'
export AIOPS_LLM_API_KEY='<RUNTIME_SECRET>'

python baseline/bian/run.py \
  --data-root data/stage1/regions \
  --event-cache outputs/v1_6/all_cities/stage1_events.json \
  --reuse-event-cache \
  --decision-backend api \
  --model <SERVER_MODEL_NAME> \
  --llm-workers 4 \
  --api-timeout 180 \
  --output outputs/v1_6/all_cities/stage1_llm_predictions.jsonl \
  --inference-log outputs/v1_6/all_cities/stage1_llm_inference.json
```

LLM 阶段至少对比：事件数、时间窗、Top1/Top5 变化、分类变化、无效输出率、重试次数和单事件延迟。不得因为 LLM 输出看起来合理，就把其结果反写成人工 Ground Truth。

## 7. 变更与验收流程

1. 数据侧先提交事实表、统计脚本、证据来源和版本信息。
2. 模型侧审查其是否属于数据事实，而非主观故障标签。
3. 模型侧更新配置或代码并先补回归测试。
4. 运行公开样例、本地小规模数据和八城全量数据。
5. 对比事件数量、时间分布、Top5、类别和资源消耗。
6. 只有官方标签或合法评测反馈可用于监督评价；无标签数据只做稳定性、一致性和分布审计。
7. 原始数据、scratch、证据缓存、事件缓存和预测结果保留在 `data/` 或 `outputs/`，由 `.gitignore` 排除；Git 只提交代码、配置、统计摘要和不含敏感信息的文档。

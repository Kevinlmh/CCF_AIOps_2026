# 模型侧与数据侧协作契约

## 共同目标

数据侧给出可复核的数据事实，模型侧把事实固化为配置、特征和测试。双方共用 `baseline/bian/preprocessing/` 的读取规则，禁止各自维护一套节点映射或空值处理逻辑。

## 请数据侧交付的内容

### 1. 文件与时间质量

按“城市 × 来源 × 文件”提供：字节数、行数、起止时间、采样间隔、重复行、缺失分钟、最大乱序分钟、空文件和表头差异。统计结果需能与模型推理日志中的 `data_audit` 对账。

### 2. 指标语义表

每个数值字段至少给出：

| 字段 | 必填说明 |
|---|---|
| source/metric | 统一名称 |
| unit | 比例、百分数、字节/秒、累计次数等 |
| kind | Gauge、Counter、State、Event |
| normal | 正常值或正常范围 |
| anomaly_direction | high、low、both、state |
| reset_rule | Counter 如何重置 |
| missing_meaning | 缺失代表无流量、采集失败或未知 |
| dimensions | 哪些 label/接口/协议共同定义一条序列 |

结论用于复核 `baseline/bian/config/metric_semantics.json` 和 `model_v1.json`，不要直接在两处重复修改。

### 3. 标识映射

汇总 `node`、`node_key`、`hostname`、`target_id`、`region` 的所有取值，并标注它们对应的官方网络元素。特别列出无法映射到 80 个候选元素的角色，说明它们是观测节点、探针还是脏数据。

### 4. 多源关联

确认下列关系：traffic 的 source/target 与服务 VM 的映射；NetFlow 采集节点与真实流量路径；FRR 日志 hostname 与路由设备；scrape target 与被采集设备；接口 ID 与拓扑链路。给出跨源时间延迟的分布，而不是只说明“有关联”。

### 5. Case 证据卡

公开三个 Case 每个形成一张证据卡：真实时间窗、根因、类别、最早直接证据、传播症状、恢复证据、反证、易混淆类别及排除理由。正式无标签数据不得人为编造答案。

## 模型侧已提供的反馈

每次流式运行的推理日志包含：

- `files`、`rows_read`、`valid_rows`、`filtered_rows`、`invalid_rows`；
- `emitted_observations`、`unknown_nodes`、`time_ranges`、`rows_conserved`；
- `observations_evaluated`、`series_state_count`、`dropped_evidence`；
- 每个事件的候选特征、分类 Top3 和触发信号。

数据侧发现统计不一致时，应按“文件清单 → 行数守恒 → 节点映射 → 指标转换 → 异常阈值”的顺序定位。

## 当前需要优先确认的问题

1. `cpu_usage` 是 0–100 百分数还是 0–1 比例；当前正式抽样显示正常中位数约 0.63、最大可接近 100。
2. `disk_io_util` 的单位和故障注入时预期幅度。
3. `ipv6_route_change_total`、`ipv6_default_route_changed_total` 和 `carrier_changes` 是否始终为累计计数器。
4. `ospf6_neighbor_state_code=6` 的状态枚举，以及异常状态的数值顺序。
5. traffic 的 `*_total` 是否每分钟采样且是否可能跨进程重置。
6. `scrape_up=0` 时同节点其他来源是否仍可信。
7. FRR 中 `sendmsg failed`、`Could not send entire message`、`Operation not permitted` 是否为环境噪声。
8. DNS/Web/Auth 分别对应哪个 `service-vm-*`，避免把关系证据同时赋给三个服务节点。
9. NetFlow 的 `if_role=\N` 是否有外部接口映射表。
10. 第一批数据是否允许多个故障时间重叠，以及同一故障是否可能跨城市传播。

## 变更流程

数据侧先在本文或独立报告中写明事实和证据；模型侧更新 JSON 配置、补回归测试并记录样例/榜单影响。原始数据、临时 SQLite 和事件缓存均保留在 `data/` 或 `outputs/`，不提交 Git。

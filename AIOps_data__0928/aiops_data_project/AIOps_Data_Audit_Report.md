# AIOps 数据侧 P0 审计交付报告

生成时间：2026-09-28（UTC）  
数据窗口：2026-08-19 04:00:00 至 2026-09-02 04:00:00 UTC，结束时间为开区间。  
审计原则：只读原始数据；不制造“正常/故障”标签；不把推断关系写成官方事实。

## 结论先行

这批数据可以交给模型侧做特征投影和规则核对，但不能把所有字段都当成“每分钟都有观测”的完整面板。最重要的结论有四个：

1. 七源全量审计已经完成：8 城、7 源、56 个城市/源输入，共 521,491,569 行。其中六类已解压源 55,824,193 行，压缩包内 NetFlow 465,667,376 行。8 个 NetFlow 城市成员全部成功读取。
2. NetFlow 的城市级时间覆盖完整：每城均为 20,160 分钟，未发现解析失败、坏行或超出数据窗口的记录；但按实体/接口序列仍有 35,475 个缺失分钟，必须保留为“无记录”，不能补成数值 0。
3. 指标语义存在两个模型风险：`node_metrics.disk_io_util` 的观测最大值达到 297.49，`cpu_usage`/`disk_io_util` 的 ratio 与 percent 口径都还不能直接假定；业务流量字段是稀疏的按流类型 schema，`missing` 很多时不等于业务失败。
4. 实体关系已经足够支持第一版跨城传播建模，但官方拓扑仍不完整：审计得到 72 个 network element、8 个 traffic observer；每城没有发现 `monitor-vm` 的原始实体记录。不能用城市名直接替代设备/接口/业务探针关系。

因此，数据侧 P0 三项已交付；P1 的 457 条模型预测逐条证据审计尚不能声称完成，因为 v2 的完整 predictions、inference log 和 feature store 不在当前工作区，只留下文档中的汇总数字和 7 条探索性候选结果。

## 1. 输入与审计口径

审计覆盖：

| 数据源 | 城市/文件数 | 行数 | 存储形式 | 主要检查 |
|---|---:|---:|---|---|
| `node_metrics` | 8 | 1,450,970 | 已解压 CSV | 时间、节点覆盖、缺失/零值、数值范围 |
| `interface_metrics` | 8 | 9,712,086 | 已解压 CSV | 接口覆盖、丢包/错误/载波变化、时间间隙 |
| `routing_metrics` | 8 | 42,139,971 | 已解压 CSV | 路由指标/标签、邻居关系、计数器变化 |
| `scrape_health` | 8 | 2,096,458 | 已解压 CSV | `scrape_up`、采样数和采集质量 |
| `traffic_flow_metrics` | 8 | 421,227 | 已解压 CSV | 业务探针、流类型稀疏性、业务症状字段 |
| `frr_syslog_events` | 8 | 3,481 | 已解压 CSV | FRR 主机、事件时间和相邻语义重复 |
| `netflow_5tuple` | 8 | 465,667,376 | 原始压缩包流式读取 | 五元组、分钟覆盖、接口/节点映射、缺失与零值 |
| **合计** | **56** | **521,491,569** | — | — |

NetFlow 的北大、上海输入是 `.aa/.ab` 分卷。审计没有把它们解压成临时大文件，而是按连续 gzip 流逐城处理。通用六源审计和 NetFlow P0 专用流式审计分别执行，最后经过独立合并脚本生成最终目录。

## 2. P0-1：八城七源质量审计

### 2.1 城市级覆盖

- NetFlow 8 城均覆盖完整 20,160 分钟，城市级 `missing_minutes_global=0`。
- `interface_metrics`、`node_metrics`、`scrape_health` 的城市级全局缺失分钟合计均为 14；`routing_metrics` 为 8；`traffic_flow_metrics` 为 151。这里的“全局”只说明文件内所有序列合并后的分钟覆盖，不代表每个实体序列都完整。
- NetFlow 按 64 个实体-接口序列审计，缺失合计 35,475 分钟，最大单次间隙为 1 分钟。模型侧必须以实体/接口序列的观测掩码区分：没有记录 ≠ 该分钟的 packets/bytes 为 0。

### 2.2 解析、坏行和相邻重复

- 七源均未发现 CSV 坏行、时间解析失败或超出预期窗口的记录。
- `frr_syslog_events` 有 42 条相邻语义重复；没有相邻完全重复行。
- NetFlow 未发现相邻完全重复或相邻语义重复。
- 重复统计是保守的相邻行统计；它不会把跨文件、跨分区或不相邻的重复自动推断出来。需要做全局去重时，应使用原始主键/语义键另行执行，而不能把这里的 0 当成“全局绝无重复”。

### 2.3 时间顺序的解释

六源的 `time_gaps.csv` 记录了实体/序列内部间隙；NetFlow 的每个城市成员还存在大量“全文件行序时间回退”，例如北大 1,378,872 次。这个数字主要来自 NetFlow 按节点、接口、流记录分块写出，不能直接解释成设备时钟漂移。

实际使用规则：

- 模型的时间窗口按 `minute_utc` 重采样；
- 时间漂移判断必须在固定实体/接口/五元组粒度内进行；
- `quality_by_city_source.csv` 的 `backward_jumps` 只能作为“原始行序不是全局时间序”的信号，不能直接作为故障特征；
- 判断采集异常时优先使用 `time_gaps.csv`、`scrape_health.scrape_up` 和实体级观测掩码。

### 2.4 计数器与零值

`counter_audit.csv` 对 `traffic_flow_metrics` 和带累计语义的路由指标执行了重复、正增量、负增量/重置检查；NetFlow 的 `packets`、`bytes`、`flow_record_count` 被按分钟 bucket measure 处理，不做累计差分。

NetFlow 实测中：

- `packets`、`bytes`、`flow_record_count` 均无缺失；
- 每条存在的 NetFlow 记录中 packets 最小为 1、bytes 最小为 48、flow record count 最小为 1；
- 因此“某实体/接口/分钟没有记录”不能填成 packets=0 或 bytes=0；应该保留 `observed=false`，必要时再聚合为无流量，但这两者不能混为一谈。

## 3. P0-2：实体与传播关系

最终关系表包含 80 个实体条目：72 个 `network_element` 和 8 个 `traffic_observer`。每城识别到 `br-1/br-2`、`cr-1/cr-2`、`fw`、`service-vm-1..3`、`traffic-vm` 等 9 个网络角色；`traffic-vm` 同时作为业务探针观察者建模。每城未在原始七源中发现 `monitor-vm` 实体，需要模型侧把它标为“配置存在但数据未观测”，不能静默补齐。

关系表中的数量与来源：

| 关系 | 数量 | 证据来源 | 使用建议 |
|---|---:|---|---|
| `owns_interface` | 482 | 同一原始 node/interface 行 | 可用于设备→接口聚合 |
| `observes_service` | 192 | traffic flow 的 source/target/domain | 业务探针→目标服务，适合传播入口 |
| `targets_city` | 192 | traffic flow 的 target_region/domain | 目标城映射是原始字段支撑 |
| `references_route_next_hop` | 181 | routing label | 直接使用 IP 邻接，需再做 IP→节点映射 |
| `references_route_peer` | 52 | routing label | 直接使用 peer 字段，保留 medium/high 证据等级 |
| `observed_netflow_interface` | 64 | NetFlow node/interface 同行 | 只能说明采集观测接口，不等同于物理拓扑 |
| `has_netflow_alias` | 32 | NetFlow node/node_key 同行 | 可用于跨源别名对齐 |
| `emits_frr_log` / `has_observed_address` | 31 / 31 | FRR hostname/source_ip | 用于 FRR 事件与设备关联 |

`entity_relations.csv` 每行都保留 `raw_fields`、`provenance`、`confidence`、`reason`。其中：

- `observed`：字段直接出现在原始记录中；
- `observed+derived`：关系由同一原始记录的明确字段确定性拼接；
- 其它模型拓扑（例如“某 IP 就是某官方节点”“某城市就是某设备”）目前没有被审计脚本擅自写入。

模型侧排序应使用“城市 + 设备/接口 + 业务目标 + 路由邻接”，不能只用城市名。否则跨城候选中出现 peak driver 与 Top1 city 不一致时，无法判断是传播方向错误、采集端观察还是目标端故障。

## 4. P0-3：关键指标语义核对

### 4.1 已能直接交给模型侧的语义

- `scrape_up`：0/1 质量状态；0 更应作为观测质量掩码，而不是业务故障标签。
- `scrape_samples`：采集样本数，适合做质量上下文；0 不等价于目标业务失败。
- `interface_metrics.rx_drop_rate/tx_drop_rate/rx_error_rate/tx_error_rate`：方向明确为高值风险，但必须保留原始单位；当前大多数城市实测为 0，西安 `rx_drop_rate` 出现非零值，最大约 0.04445。
- NetFlow `packets`、`bytes`、`flow_record_count`：分钟 bucket measure，适合按 entity/interface/protocol 聚合；缺记录的语义是 no record，不是数值零。
- traffic flow 的 latency/loss/failure/timeout/request 字段：按流类型提供服务症状或分母上下文，缺失通常表示该流类型不适用或该序列未产生观测，不能直接归为正常或故障。

### 4.2 必须在模型侧先确认再用阈值的语义

- `node_metrics.cpu_usage` 最大为 100，当前不能只凭字段名决定它是 ratio 还是 percent；应把转换写成显式契约，确认后再与 v2 的阈值比较。
- `node_metrics.disk_io_util` 最大为 297.49，明显不能未经解释地当作 [0,100] 百分比。可能是导出定义、聚合口径或字段命名与模型假设不同；在确认前不得把 `>55` 直接写成磁盘故障标签。
- `disk_read_rate`/`disk_write_rate` 的单位和采样含义仍应与采集端定义核对；审计只证明观测范围，不把范围反推成官方单位。
- `routing_metrics.value` 必须保留 `metric_name` 与 `label` 维度；路由状态、prefix 数和 route change counter 不能混成一列无维度的数值。

## 5. 模型侧交付边界

当前工作区能确认的模型侧材料是：

- `CCF_AIOps_2026` v2 工程及其设计文档存在；
- 文档中记录了 457 条候选、40 条 `link.delay`、跨城峰值驱动冲突等汇总信息；
- 当前数据项目只有 7 条探索性候选结果，以及公开的 3 条样例归因。

但没有找到可逐条复核的：

- 457 条 predictions 原始文件；
- 对应 inference log；
- 与该轮运行一一对应的 feature store；
- 40 条 `link.delay`、136 条类别信息不足、跨城 Top1 冲突的逐条证据链。

所以本交付不伪造 P1 结论，也不把文档汇总数字写成新的故障标签。拿到这三类模型产物后，下一步应按每条预测输出：原始字段、前后时间线、支持证据、冲突证据、不足原因，并只写“支持/冲突/不足”，不写成事实故障标签。

## 6. 交付文件

完整七源结果：

- [audit_manifest.json](outputs/data_audit_20260928_full/audit_manifest.json)
- [audit_detail.json](outputs/data_audit_20260928_full/audit_detail.json)
- [quality_by_city_source.csv](outputs/data_audit_20260928_full/quality_by_city_source.csv)
- [field_quality.csv](outputs/data_audit_20260928_full/field_quality.csv)
- [entity_coverage.csv](outputs/data_audit_20260928_full/entity_coverage.csv)
- [entity_registry.csv](outputs/data_audit_20260928_full/entity_registry.csv)
- [entity_relations.csv](outputs/data_audit_20260928_full/entity_relations.csv)
- [service_probe_mapping.csv](outputs/data_audit_20260928_full/service_probe_mapping.csv)
- [metric_semantics.csv](outputs/data_audit_20260928_full/metric_semantics.csv)
- [time_gaps.csv](outputs/data_audit_20260928_full/time_gaps.csv)
- [counter_audit.csv](outputs/data_audit_20260928_full/counter_audit.csv)
- [source_summary.csv](outputs/data_audit_20260928_full/source_summary.csv)

可复现代码：

- [run_data_audit.py](run_data_audit.py)：完整入口；
- [data_audit.py](pipeline/data_audit.py)：六源通用审计和关系/语义逻辑；
- [netflow_audit_fast.py](pipeline/netflow_audit_fast.py)：NetFlow P0 流式审计；
- [merge_audit_outputs.py](pipeline/merge_audit_outputs.py)：结果合并和最终 manifest 生成。

## 7. 明确的下一步

1. 模型侧先消费 `metric_semantics.csv` 和 `entity_relations.csv`，确认 CPU/disk 单位、路由 label 维度和 NetFlow 缺失掩码后，再锁定特征投影。
2. 补交 v2 的 457 条预测、inference log、feature store 后，执行 P1 逐条证据审计。
3. 在拿到官方物理拓扑或接口映射后，补齐 `monitor-vm` 以及 IP/接口到官方 ID 的关系；在此之前不要把推断关系升级为官方拓扑。

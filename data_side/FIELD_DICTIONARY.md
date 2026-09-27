# 七源字段与模型变换口径

完整原始字段名见 [raw_schema.csv](results/raw_schema.csv)；[校订后的逐字段字典](results/field_dictionary_reconciled.csv) 覆盖七源全部 207 个字段，合并了 `AIOps_data_dyx` 既有释义与本轮逐行缺失审计。既有释义属于项目解释，并非主办方正式数据字典。下表记录当前代码**实际采用**的身份和数值语义；未由主办方数据字典确认的物理单位不在这里猜测。

| 源 | 行身份与时间 | 数值/文本处理 | 特征投影与注意事项 |
| --- | --- | --- | --- |
| `node` | `timestamp`、`region`、`node`/`node_type` | CPU、负载、可用内存比例、磁盘、文件系统、进程等宽表指标 | 投影为 node 分钟单元；有显式观测 mask。比例和百分比不能混用，按原字段量程解释。 |
| `interface` | `timestamp`、网元、`interface_id`、`if_role` | 接口收发、丢包、错误、载波变化 | 投影到 node，同时在维度旁路保留接口 ID；一个网元的不同接口不应共享基线。接口 ID 本身不等于链路对端。 |
| `routing` | `timestamp`、网元、`metric_name`、`label` | 长表 `value` 数值；`label` 解析出 peer、state、接口、prefix 等维度 | 投影到 node，维度序列另存。状态变化时动态 `state` 标签不能误当成两个独立的稳定邻居。 |
| `scrape` | `timestamp`、网元、`target_id`、`exporter_type` | `scrape_up`、采集耗时、样本数；`scrape_error` 留作文本 | 表示观测健康；采集失败期间的其他指标可能是陈旧值。一个网元可对应多个 exporter，聚合后的 `scrape_up` 不能替代逐 exporter 诊断。 |
| `traffic` | `timestamp_utc`、`series_key`、源/目标区域、域名、`flow_type` | 带 `_total`、`_sum`、`_count` 或 histogram bucket 的计数器先按真实时间间隔差分；支持 reset；派生请求数和成功/错误比例 | 投影为从 `source_region-traffic-vm` 到 `service-group:<target_region>:<flow_type>` 的边。计数器的 `_rate` 是分钟变化口径，不等于原 CSV 累积值；同一业务的不同 `series_key` 不共享差分状态。 |
| `netflow` | `minute_utc`、`node_key`/`node`、`interface_id`、`protocol` 和五元组 | 按分钟、网元、接口、协议用 SQLite 聚合包、字节、流数、端点/端口基数，计算协议字节占比 | 投影为网元到接口的边，主要用于辅助定位；五元组中的 IP/端口并不直接证明官方根因网元。 |
| `frr` | `event_time`（必要时回退 `received_at`）、hostname/region、severity/program | 解析日志事件计数并保留原始文本证据 | 投影为 log 分钟单元；日志稀疏，不能把无日志等同于正常。 |

所有源先把原始设备标识归一到官方 8 城 × 10 角色的 80 个合法网元；无法映射、错误时间、非法数值及乱序行由构建审计分别记录。模型张量按分钟对齐，但 FRR 原始事件保留秒级时间。

### 常用字段释义

| 字段/字段族 | 含义和已确认的量程口径 |
| --- | --- |
| `node.cpu_usage`、`node.disk_io_util` | 设备 CPU/磁盘 I/O 利用率；现有直接检测按百分数型指标处理。旧版 DYX 字典称两者为 `ratio`，但观测值分别达到 100 和 297.493，不能按 0–1 解释；尤其 `disk_io_util` 超 100 的含义仍需导出方确认。 |
| `node.memory_available_ratio`、`node.filesystem_used_ratio`、`node.inode_used_ratio`、`node.open_fd_ratio` | 分别是可用内存、文件系统空间、inode、文件描述符的比例字段；异常方向不相同。 |
| `node.load1`、`node.load5`、`node.process_count` | 1/5 分钟系统负载与进程数量；不是 CPU 利用率的同义列。 |
| `interface.rx_*`、`interface.tx_*` | 分别为接收/发送方向的流量、包、丢包和错误速率；`carrier_changes` 为链路载波状态变化计数。字节/包速率的实际时间单位需以导出说明核准。 |
| `routing.metric_name`、`routing.label`、`routing.value` | 指标名称、Prometheus 风格维度标签和数值；具体含义必须连同 `metric_name` 及标签一起解释，不能把所有 `value` 混成一个指标。 |
| `scrape.scrape_up`、`scrape_duration_seconds`、`scrape_samples`、`scrape_error` | 分别是采集成功状态、采集耗时秒数、采样量、错误文本；`scrape_up=0` 是观测质量问题，并不自动等于网元故障。 |
| `*_flow_requests_total`、`*_flow_success_total`、`*_flow_error_total` | Traffic 累计请求/成功/失败计数器；先对同一 `series_key` 做时间差分，派生分钟请求与成功/失败量。 |
| `*_flow_latency_p95_seconds`、`*_flow_latency_mean_seconds` | 业务时延秒数；前者是 p95，不能与均值或累计 `duration_seconds_sum` 混用。 |
| `*_flow_*_bucket_le_*` | Traffic 直方图累计桶；按同一序列差分后才是当期桶变化量。 |
| `netflow.packets`、`netflow.bytes`、`flow_record_count` | 一分钟五元组记录的包、字节、流记录量；经接口和协议聚合后形成特征。 |
| `frr.severity`、`frr.program`、`frr.message` | 日志严重度、产生程序和文本；重复日志可能不是独立故障。 |

原始表含 207 个字段名（各源分别为 16、15、7、9、126、19、15），逐字段原名和城市覆盖见字段矩阵。字段名本身不足以确认所有物理单位、计数器是否重置及某指标的故障方向；这些解释应由数据方提供的导出字典补充。

**缺失语义**：原始源中的空值、`NULL`、`NA` 和 `NaN` 均计入 [逐字段缺失表](results/raw_field_quality_by_source.csv)。`netflow.if_role` 全空，不能用于角色解释；`interface.if_role` 大多数为空。`routing.value` 必须按 `metric_name × node_type` 判读，CR 上的 BGP 值缺失并不表示数值为零。Traffic 的 126 列按不同 `flow_type` 共用一张宽表，应使用 [活动流条件缺失表](results/traffic_flow_field_quality_by_flow.csv)，而不是将其他流类型留空的列当作采集失败。Elephant 活动流里的时延均值、p95 和 QPS 全空，直接检测不能从这些字段取得有效证据。

`metric_profile.csv` 给的是**投影后**的观测值及 `|x_t-x_{t-1}|` 分位数，只有连续两个分钟均观测到该实体与指标时才计算变化。`dimension_series_delta.csv` 先在每条稳定维度序列内求变化的 p99，再按城市/源/指标汇总这些逐序列 p99 的中位数、p90 和最大值；它不是所有单元混在一起的全局 p99。两个文件都包含故障和未知质量的时间段，不能直接命名为“正常波动阈值”。

代码依据：[原始解析](/Users/likevin/lmh/CCF_AIOps_2026/aiops_v2/data/source.py)、[多源字段规则](/Users/likevin/lmh/CCF_AIOps_2026/baseline/bian/preprocessing/multisource.py)、[特征投影](/Users/likevin/lmh/CCF_AIOps_2026/aiops_v2/data/projection.py)。

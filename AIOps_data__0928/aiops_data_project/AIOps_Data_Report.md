# AIOps 挑战赛数据工作报告

> 报告主题：数据基本情况统计、数据 pipeline、字段语义、多源数据源头与归因 Case 分析  
> 数据范围：2026-08-19 04:00:00 至 2026-09-02 04:00:00  
> 覆盖区域：北大、上海、成都、广州、南京、沈阳、武汉、西安，共 8 个区域

---

## 一、数据 pipeline

> 本部分只展示数据从输入到诊断输出的处理 pipeline，不展示本地文件目录、压缩包名称或解压路径。

### 1.1 Pipeline 输入与输出

![数据 pipeline 总览](figures/fig_01_pipeline_overview.png)

图 1. 从原始数据到候选根因归因的数据 pipeline。

Pipeline 的输入是 8 个区域的多源网络观测数据和官方已有 Case 的时间窗口，观测源包含主机资源、接口、路由、采集健康、服务流量、FRR 日志和五元组流量等。Pipeline 的输出是针对官方 Case 的证据解释，包括候选根因网元、故障分类原因、字段证据、传播关系和反证。

核心输出对象为：

```text
多源观测数据
    → 规范化观测数据
    → 官方 Case 时间窗口对齐
    → 窗口内多源证据提取
    → Top-5 根因解释 + 故障分类原因
```

### 1.2 数据 pipeline 设计

本项目形成的 pipeline 分为五个主要阶段：

```text
P0 输入数据登记
   数据源、时间范围、区域、实体和字段契约
        ↓
P1 数据 inventory
   表头、行数、时间范围、缺失值、实体基数、质量问题
        ↓
P2 逻辑规范化
   UTC 时间、缺失值、城市名称、网元角色、指标字段
        ↓
P3 官方 Case 对齐
   官方 Case 时间窗 → 8 个区域 × 全部观测源
        ↓
P4 证据融合
   异常强度、时间先后、多源一致性、网元角色、参考拓扑
        ↓
P5 输出
   Top-5 候选根因、故障分类、字段证据、解释文本
```

主要处理规则如下：

1. 原始 CSV 只承担观测事实，不要求包含官方 `case_id`、`root_cause` 或 `fault_category`；
2. 官方 Case 的 `start_time` 和 `end_time` 是第四部分的窗口边界，不由本项目重新切分；
3. 每个官方 Case 都从 8 个区域和全部可用观测源提取窗口内证据；
4. 候选网元集合按照公开网络元素和参考拓扑建立，不能只分析官方根因所在城市；
5. 计数器、采集状态和日志按照字段语义解释，避免把观测中断误认为设备故障；
6. 归因时同时保留直接证据、时间先后、传播关系和排除其他类别的反证。

项目运行入口为：

```bash
python3 aiops_data_project/run_pipeline.py
```

本报告后续章节给出字段语义、多源数据源头和官方 Case 归因方法；机器可读结果包括字段字典、源头映射、官方样例 Case 归因解释，以及用于汇报的 PNG/PDF 图。`candidate_cases.*` 仅作为连续原始数据的探索性审计结果保留。

---

## 二、字段统计与字段含义

### 2.1 字段统计概览

已解压数据共统计 188 个真实观测字段，分布如下：

![数据源字段数量](figures/fig_03_schema_dimensionality.png)

图 3. 各数据源字段数量。

| 数据源 | 字段数 | 字段组织方式 |
|---|---:|---|
| `node_metrics` | 16 | 宽表，每行对应一个网元和一个时间点 |
| `interface_metrics` | 15 | 宽表，每行对应一个接口和一个时间点 |
| `routing_metrics` | 7 | 长表，字段含义由 `metric_name + label + value` 共同决定 |
| `scrape_health` | 9 | 采集状态宽表 |
| `traffic_flow_metrics` | 126 | 基础字段 + DNS/Web/Auth/Elephant 重复指标组 |
| `frr_syslog_events` | 15 | 路由软件离散事件表 |
| `netflow_5tuple` | 19 | schema reference，明文暂未展开 |

完整字段字典见：

- [field_dictionary.csv](outputs/field_dictionary.csv)：已解压数据中实际观测到的字段；
- [field_dictionary.json](outputs/field_dictionary.json)：同内容的 JSON 版本；
- [schema_reference.csv](outputs/schema_reference.csv)：包含 netflow 在内的契约字段定义。

### 2.2 `node_metrics`：主机资源字段

| 字段 | 含义 | 诊断作用 |
|---|---|---|
| `timestamp` | 主机指标观测时间 | 时间关联键 |
| `region` | 区域或城市 | 区域关联键 |
| `node` | 被观测网元 | 根因候选实体 |
| `node_type` | 网元角色 | 解释网元功能 |
| `cpu_usage` | CPU 使用率 | 判断 CPU 压力 |
| `load1` / `load5` | 1 分钟/5 分钟系统负载 | 判断处理队列压力 |
| `memory_available_ratio` | 可用内存比例 | 判断内存压力 |
| `swap_used_ratio` | Swap 使用比例 | 判断内存不足后的交换行为 |
| `disk_read_rate` / `disk_write_rate` | 磁盘读写速率 | 判断磁盘 I/O 压力 |
| `disk_io_util` | 磁盘 I/O 利用率 | 判断磁盘饱和 |
| `filesystem_used_ratio` | 文件系统使用比例 | 判断磁盘空间不足 |
| `inode_used_ratio` | inode 使用比例 | 判断 inode 耗尽风险 |
| `open_fd_ratio` | 文件描述符使用比例 | 判断进程资源耗尽 |
| `process_count` | 进程数量 | 判断进程异常增长 |

### 2.3 `interface_metrics`：接口和链路字段

| 字段 | 含义 | 诊断作用 |
|---|---|---|
| `timestamp` | 接口指标时间 | 时间关联键 |
| `region` | 区域 | 区域关联键 |
| `node` | 接口所属网元 | 根因候选实体 |
| `node_type` | 网元角色 | 区分边界路由器、核心路由器、防火墙等 |
| `interface_id` | 接口标识 | 定位具体接口 |
| `if_role` | 接口角色 | 判断接口连接方向或功能 |
| `rx_bytes_rate` | 接收字节速率 | 流量强度背景 |
| `tx_bytes_rate` | 发送字节速率 | 流量强度背景 |
| `rx_packets_rate` | 接收包速率 | 流量强度背景 |
| `tx_packets_rate` | 发送包速率 | 流量强度背景 |
| `rx_drop_rate` | 接收丢包速率 | 链路丢包证据 |
| `tx_drop_rate` | 发送丢包速率 | 链路丢包证据 |
| `rx_error_rate` | 接收错误速率 | 接口错误证据 |
| `tx_error_rate` | 发送错误速率 | 接口错误证据 |
| `carrier_changes` | 载波变化次数 | 判断接口抖动或物理链路不稳定 |

这里必须区分“流量升高”和“链路故障”：`rx_bytes_rate` 或 `tx_bytes_rate` 增大只能说明流量增加，不能单独说明发生了链路错误。因此最终 Case 检测没有让这些字段单独触发链路故障。

### 2.4 `routing_metrics`：路由控制面字段

| 字段 | 含义 | 诊断作用 |
|---|---|---|
| `timestamp` | 路由指标时间 | 时间关联键 |
| `region` | 路由指标区域 | 区域关联键 |
| `node` | 路由指标所属网元 | 根因候选实体 |
| `node_type` | 路由设备角色 | 区分 BR、CR 等角色 |
| `metric_name` | 指标名称 | 区分 BGP、OSPF6、IPv6 route 指标 |
| `label` | peer、命令、前缀、接口等上下文 | 解释指标具体对象 |
| `value` | 指标值 | 状态、数量或计数值 |

`routing_metrics` 必须联合解释。例如：

- `metric_name=bgp_peer_up`、`value=0` 表示 BGP peer 不处于 up 状态；
- `metric_name=bgp_peer_prefix_received`、`value=0` 表示该 peer 没有接收到路由前缀；
- `metric_name=ospf6_neighbor_state_code` 用于判断 OSPF6 邻居状态；
- `label` 中的 peer、remote AS、state 和 interface 用于进一步定位具体邻居。

### 2.5 `scrape_health`：观测质量字段

| 字段 | 含义 | 诊断作用 |
|---|---|---|
| `timestamp` | 采集尝试时间 | 时间关联键 |
| `region` | 采集区域 | 区域关联键 |
| `target_id` | 被采集目标 | 采集对象定位 |
| `node` | 采集目标网元 | 观测实体 |
| `exporter_type` | node exporter 或 routing exporter | 识别采集来源 |
| `scrape_up` | 是否采集成功，0/1 | 判断观测中断 |
| `scrape_duration_seconds` | 采集耗时 | 判断采集延迟或阻塞 |
| `scrape_samples` | 返回样本数 | 判断采集内容是否异常 |
| `scrape_error` | 采集错误信息 | 解释采集失败原因 |

`scrape_up=0` 是观测质量证据，不应直接被当作设备根因。

### 2.6 `traffic_flow_metrics`：业务和服务流量字段

该数据源包含 126 列，主要由以下字段组构成：

| 字段组 | 含义 |
|---|---|
| `id`、`timestamp_utc` | 记录标识和观测时间 |
| `source_region`、`source_ip` | 探针来源区域和地址 |
| `target_region`、`target_domain` | 目标服务区域和域名 |
| `flow_type` | DNS、Web、Auth、Elephant 等流量类型 |
| `protocol` | 应用或传输协议 |
| `*_observed_qps` | 观测 QPS |
| `*_latency_mean_seconds` | 平均延迟 |
| `*_latency_p95_seconds` | P95 延迟 |
| `*_loss_rate` | 丢失比例 |
| `*_jitter_seconds` | 延迟抖动 |
| `*_throughput_bps` | 吞吐率 |
| `*_failed_total`、`*_timeout_total` | 累计失败和超时数，需要差分 |
| `*_requests_total` | 累计请求数，需要差分 |

其中 `*_total`、`*_count`、`*_sum` 一般是累计量，不能直接当作当前一分钟的故障强度，应该转换为差分或窗口速率。

### 2.7 `frr_syslog_events`：路由软件事件字段

| 字段组 | 含义 |
|---|---|
| `id` | 事件记录编号 |
| `received_at` | 日志接收时间 |
| `event_time` | 日志实际发生时间，用于 Case 对齐 |
| `received_at_raw`、`event_time_raw` | 原始时间字符串，用于审计 |
| `source_ip`、`hostname`、`region` | 事件来源和网元定位 |
| `facility`、`severity`、`severity_code` | syslog 设施和严重级别 |
| `program` | `bgpd` 或 `ospf6d` 等 FRR 进程 |
| `pid` | 进程号 |
| `message` | 具体路由软件事件内容 |
| `inserted_at` | 入库时间 |

归因时使用 `event_time`，不能用 `received_at` 或 `inserted_at` 替代故障发生时间。

### 2.8 `netflow_5tuple`：五元组流量字段

netflow 的字段定义已经写入 `outputs/schema_reference.csv`。核心字段包括：

| 字段 | 含义 |
|---|---|
| `minute_utc` | 流量聚合分钟 |
| `region`、`region_code` | 采集区域 |
| `node_key`、`node` | 流量观测网元 |
| `interface_id`、`if_role` | 观测接口及其角色 |
| `protocol` | IP 协议号 |
| `src_addr`、`src_port` | 源地址和源端口 |
| `dst_addr`、`dst_port` | 目的地址和目的端口 |
| `packets` | 该分钟的包数 |
| `bytes` | 该分钟的字节数 |
| `flow_record_count` | 底层流记录数量 |
| `first_seen`、`last_seen` | 流在时间桶中的首末出现时间 |

---

## 三、多源数据源头及其含义

![多源证据支持矩阵](figures/fig_04_evidence_matrix.png)

图 4. 多源数据对不同故障族的证据支持关系。权重是初始证据权重，不是监督学习标签。

| 数据源 | 推断源头 | 观测粒度 | 对应含义 | 主要支持的故障族 |
|---|---|---|---|---|
| `node_metrics` | node exporter 或主机资源采集器 | 每分钟、网元级 | 设备主机资源状态 | resource |
| `interface_metrics` | 网络接口 exporter | 每分钟、接口级 | 接口流量、丢包、错误、载波 | link、firewall、routing |
| `routing_metrics` | 路由 exporter | 每分钟、网元/指标/label 级 | BGP、OSPF6、IPv6 路由控制面状态 | routing |
| `scrape_health` | Prometheus scrape health | 每分钟、目标级 | exporter 是否能够正常采集 | observability |
| `traffic_flow_metrics` | DNS/Web/Auth 合成探针或服务流量监控 | 服务流级 | 用户侧延迟、吞吐、失败、超时 | service、link、firewall |
| `frr_syslog_events` | FRR 的 `bgpd`、`ospf6d` syslog | 不规则离散事件 | 路由软件状态变化和错误消息 | routing |
| `netflow_5tuple` | 流量采集器或 NetFlow 类 collector | 每分钟、五元组级 | 地址、端口、协议、包和字节流量 | link、firewall、service、routing |

多源数据在诊断中的关系不是简单的“所有字段拼在一起”，而是具有不同的证据层级：

```text
node_metrics       → 设备资源状态
interface_metrics  → 接口和链路状态
routing_metrics    → 路由控制面状态
frr_syslog_events  → 路由软件离散事件
scrape_health      → 观测是否可信
traffic_flow       → 业务和服务侧症状
netflow            → 五元组级流量事实
```

因此，根因判断需要遵循以下原则：

1. 资源指标先异常，可能说明设备资源压力是根因；
2. 路由控制面先出现 peer 或邻居状态变化，通常比业务流量异常更接近路由根因；
3. 业务流量异常一般是用户侧症状，不能直接作为服务进程根因；
4. 接口流量升高不等于接口故障，必须结合丢包、错误或载波变化；
5. `scrape_up=0` 需要被标记为观测不可靠，而不是直接赋予故障类别；
6. netflow 适合用来确认端口、地址和五元组传播范围，但不应该未经聚合直接送入模型。

---

## 四、官方 Case 的归因解释：为什么定位到这个错误、为什么分类成这个错误

### 4.1 任务边界：不从原始观测数据重新构造 Case

当前原始 CSV 是多源观测数据，记录的是时间、网元、指标、日志和流量事实；它本身不需要包含 `case_id`、`root_cause` 或 `fault_category`。这些字段属于官方 Case / 评测标注层，而不是观测数据层。

因此，本项目第四部分不负责从连续 14 天观测中自行切分故障 Case，也不把探索性异常窗口当作官方 Case。正确的输入关系是：

```text
官方已有 Case
    ├── Case ID
    ├── start_time / end_time
    ├── 官方根因网元（用于结果核对）
    └── 官方故障分类（用于结果核对）
             ↓
原始多源观测数据在该时间窗口内的证据提取
             ↓
解释为什么定位到该网元、为什么属于该分类
```

公开 baseline 的 `sample/ground_truth.jsonl` 已明确展示了这种 Case 记录格式，例如 `ground_truth_id`、时间窗口、`root_cause.network_element_id` 和 `fault_category`。正式分析时，官方提供的 Case 文件应作为 Case 边界和答案核对依据；不能用数据负责人自行检测出的 7 个候选窗口替代官方 Case。

### 4.2 官方 Case 的归因解释步骤

对每一个官方 Case，只做窗口内的证据解释，不重新定义 Case：

1. **窗口对齐**：使用官方 `start_time` 和 `end_time`，在 8 个区域的所有观测源中提取同一时间范围的数据；
2. **候选网元对齐**：按照公开拓扑列出全部候选网元，包括没有直接异常记录的网元；
3. **直接证据提取**：收集 `node_metrics`、`interface_metrics`、`routing_metrics`、`traffic_flow_metrics`、`frr_syslog_events` 和可用的 netflow 证据；
4. **定位原因解释**：优先检查是否存在与故障机制直接对应的字段，并比较候选网元之间的异常强度、首次出现时间和局部性；
5. **传播关系检查**：判断其他网元的异常是根因证据，还是根因产生后的下游症状；
6. **分类解释**：根据故障机制对应的字段族决定大类和子类，不根据网元名称或异常值大小直接分类；
7. **反证记录**：说明为什么没有选择相近但不正确的类别，例如 CPU 波动为什么不优先于磁盘 I/O 饱和，或业务错误为什么不优先于路由控制面异常。

归因解释必须回答两个互相独立的问题：

```text
为什么是这个网元？
    看位置、时间先后、直接字段证据和传播关系

为什么是这个类别？
    看故障机制、字段含义和排除其他类别的反证
```

### 4.3 公开 baseline 样例 Case 的归因说明

当前工作目录中的 14 天原始数据没有随数据表附带官方 Case 标签；因此下面使用公开 baseline 随附的 3 个官方样例 Case 展示正确的归因写法。这些样例不是当前 14 天数据重新构造的 Case，而是官方已有 Case 的窗口解释示例。

| 官方 Case | 官方根因网元 | 官方分类 | 定位依据 | 分类依据 |
|---|---|---|---|---|
| `incident-0013` | `xian-service-vm-1` | `resource/cpu_pressure` | 官方窗口内 `cpu_usage`、`load1`、`load5` 同时突变，且直接发生在 `xian-service-vm-1`；内存比例基本稳定。 | CPU 使用和主机负载属于计算资源压力证据，因此不是链路、路由或服务协议类别。 |
| `incident-0017` | `guangzhou-service-vm-3` | `resource/memory_pressure` | `memory_available_ratio` 从窗口前约 `0.9234` 降至约 `0.8505–0.8552`，直接指向该 service-vm 的可用内存下降。 | 可用内存比例是内存压力的直接字段；CPU 同步变化只能作为伴随现象，不能替代内存分类。 |
| `incident-0021` | `wuhan-service-vm-2` | `resource/disk_io_pressure` | `disk_io_util` 在官方窗口内达到约 `85.45–100%`，读写速率同时出现数量级变化，证据集中在该网元。 | 磁盘利用率、读速率和写速率共同指向存储子系统饱和；CPU/load 升高更可能是 I/O 等待的伴随症状。 |

逐 Case 的窗口统计和解释字段见：[official_case_attribution_examples.json](outputs/official_case_attribution_examples.json)。

### 4.4 归因解释的统一模板

对正式 Case 建议按照以下模板填写：

```text
Case ID：<官方 Case ID>
官方时间窗：<start_time> — <end_time>
官方根因网元：<network_element_id>
官方故障分类：<major_category>/<sub_category>

为什么定位到该网元：
1. 该网元首先或最直接出现了什么字段异常；
2. 该字段为什么比其他候选网元的异常更接近根因；
3. 其他异常网元为什么更像传播后的症状；
4. 参考拓扑和多源时间线是否支持该判断。

为什么分类成该错误：
1. 该分类对应的故障机制是什么；
2. 哪些字段直接支持该机制；
3. 哪些相邻分类被反证排除；
4. 日志、流量、接口或资源证据是否形成一致的时间链。
```

`candidate_cases.json` 仍然保留在项目中，但它只是连续原始数据上的探索性异常审计结果，不是第四部分的官方 Case 归因交付物。

---

## 五、结论

本项目已经完成四项工作：

1. 完成 8 个区域、48 个 CSV 文件、55,824,193 行数据的基本统计，并形成了可复现 pipeline；
2. 完成已解压数据的 188 个字段统计和字段语义说明，并补充了 netflow 的 schema reference；
3. 梳理了 node、interface、routing、scrape、traffic、FRR syslog、netflow 七类数据源的产生机制和诊断作用；
4. 基于官方 Case 的时间窗和根因/分类结果，建立了“窗口对齐—直接证据—传播关系—反证排除”的归因解释方法，并对公开 baseline 的 3 个样例 Case 给出了定位和分类原因。

当前数据侧的核心交付为：

```text
原始数据登记
+ 数据质量统计
+ 字段语义字典
+ 多源关系映射
+ 官方 Case 时间窗口对齐
+ Top-1 / Top-5 根因解释
+ 字段级证据
+ 分类理由与反证
+ 不确定性说明
```

这套结果可用于特征工程、时序检测、拓扑推理、根因排序和比赛提交格式转换。

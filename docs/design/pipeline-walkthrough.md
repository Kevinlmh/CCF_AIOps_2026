# Pipeline 说明

---

## 1. 输入与输出格式

### 1.1 输入文件类型

输入是CSV，按城市分目录，每个城市7个文件：

| 文件 | 一行代表 | 数值列 |
|---|---|---|
| node_metrics | 一台设备一分钟的整机快照（宽表） | cpu_usage, load1, load5, memory_available_ratio, swap_used_ratio, disk_read_rate, disk_write_rate, disk_io_util, filesystem_used_ratio, inode_used_ratio, open_fd_ratio, process_count |
| interface_metrics | 一个接口一分钟（宽表） | rx/tx_bytes_rate, rx/tx_packets_rate, rx/tx_drop_rate, rx/tx_error_rate, carrier_changes |
| routing_metrics | 一个路由指标的一个 label 组合（长表） | value（指标名在 `metric_name` 列，标签在 `label` 列） |
| scrape_health | 一个采集目标一分钟 | scrape_up, scrape_duration_seconds, scrape_samples |
| traffic_flow_metrics | 一条业务流一分钟（长表宽列，120+列） | 按 flow_type 分 dns/web/auth/elephant 四块，块内形如 `auth_flow_requests_total`、`auth_flow_latency_p95_seconds` |
| netflow_5tuple_minute_readable | 已按分钟聚合的五元组（长表） | packets, bytes, flow_record_count |
| frr_syslog_events | 一条路由器日志 | 无（文本表） |

### 1.2 一行 CSV 如何变成模型输入

模型消费的最小单位不是CSV行，而是观测。

一行CSV里有几个数值列，就拆成几条观测；每条观测按 `(数据源, 网元, 指标, 维度)` 四项归组，同一个键上的观测按时间排起来，就是一条时间序列。检测器看到的输入就是这一堆序列。

四项里第四项**维度**可以为空——六个数值源里只有 node 的维度是空的，其余五类都带维度，用来区分同网元同指标下的不同对象（哪个接口、哪个 peer、哪条业务流）；frr 是文本源，不产生数值观测。四种组合全同才算同一条序列，缺一项都不行。

**（一）宽表：一行拆成多条观测，分别进入多条序列**

`node_metrics.csv` 的前两行：

```text
timestamp             node   cpu_usage  load1  process_count    …
2026-08-19 04:00:00   br-1   0.694      0.02   195              …
2026-08-19 04:01:00   br-1   0.731      0.03   195              …
```

第一行有12个数值列，拆成12条观测：

```text
第 1 行（04:00）
  ├─ 观测：时间=04:00  源=node  网元=chengdu-br-1  指标=node.cpu_usage      值=0.694
  ├─ 观测：时间=04:00  源=node  网元=chengdu-br-1  指标=node.load1          值=0.02
  ├─ 观测：时间=04:00  源=node  网元=chengdu-br-1  指标=node.process_count  值=195
  └─ …（共 12 条）

第 2 行（04:01）同样拆成 12 条
```

按 `(源, 网元, 指标, 维度)` 归组后得到 12 条序列。node 数据的维度为空，所以这 12 条的键差别只在第三项：

```text
node.cpu_usage      @ chengdu-br-1 →  [0.694, 0.731, …]
node.load1          @ chengdu-br-1 →  [0.02,  0.03,  …]
node.process_count  @ chengdu-br-1 →  [195,   195,   …]
…
```

**（二）带维度时，同一分钟多行进入多条不同序列**

`interface_metrics.csv` 的前四行：

```text
timestamp             node   interface_id  rx_bytes_rate
2026-08-19 04:00:00   br-1   ens4          2104.7
2026-08-19 04:00:00   br-1   ens5           784.7
2026-08-19 04:00:00   br-1   ens10              0
2026-08-19 04:01:00   br-1   ens4           795.6
```

前三行的源、网元、指标三项完全相同，差别在第四项维度里的 `interface_id`。四项不全同就是不同的序列，于是分出三条：

```text
interface.rx_bytes_rate, interface_id=ens4  @ chengdu-br-1 → [2104.7, 795.6, …]
interface.rx_bytes_rate, interface_id=ens5  @ chengdu-br-1 → [ 784.7, …]
interface.rx_bytes_rate, interface_id=ens10 @ chengdu-br-1 → [     0, …]
```

**（三）长表：一行只产出一条观测**

`routing_metrics.csv` 把指标名放在数据里：

```text
timestamp             node   metric_name          label                   value
2026-08-19 04:00:00   br-1   bgp_command_success  command="bgp_summary"   1
```

一行产出一条观测：

```text
时间=04:00  源=routing  网元=chengdu-br-1  指标=routing.bgp_command_success  值=1  维度={command: bgp_summary}
```

**（四）traffic：一行描述一条业务流**

traffic 表没有 node 列。它描述的不是某台设备的指标，而是从哪个地域到哪个地域的业务流。以一行 auth 流为例，只列有值的列：

```text
timestamp_utc  = 2026-08-19 04:00:00
flow_type      = auth                              ← 本行属于 auth 块
source_region  = chengdu                           ← 流量从成都发出
target_region  = wuhan                             ← 打向武汉
target_domain  = auth01.wuhan.aiops.local
protocol       = http
series_key     = 4008dbc3...                       ← 这条业务流的哈希标识
── 下面是 auth_flow_* 块，共 31 个字段有值 ──
auth_flow_requests_total        = 413191
auth_flow_success_total         = 408761
auth_flow_error_total           = 4424
auth_flow_latency_p95_seconds   = 0.2999
…（另有 dns_flow_* / web_flow_* / elephant_flow_* 三块，本行全是 \N）
```

这一行的 31 个字段拆成 31 条观测。其中一条是：

```text
观测：时间=04:00  源=traffic  网元=chengdu-traffic-vm
      指标=traffic.auth.latency_p95_seconds   值=0.2999
      维度={flow_type: auth, protocol: http, series_key: 4008dbc3…,
            source_region: chengdu, target_region: wuhan,
            target_domain: auth01.wuhan.aiops.local}
      关联节点=(wuhan-service-vm-1, wuhan-service-vm-2, wuhan-service-vm-3)
```

两处需要解释：

**网元为什么是 `chengdu-traffic-vm`。** traffic 表里没有设备列，只有采集地域。这条流量的观测点在成都，所以网元记作「源地域 + traffic-vm」。也就是说，这条证据是「成都测得的一条业务流」，而不是「某台被观测设备上的一个指标」。七类数据里只有 traffic 是这样，其余六类的网元都直接来自设备列。

**关联节点是什么。** 这条流量的目标是武汉的认证服务。它出问题，可能是成都这边的出口有问题，也可能是武汉的服务端有问题，而这一行数据判断不了。代码于是把武汉的三个服务网元作为待选挂上，交给后面的定位环节去排。这是七类数据里唯一一处跨地域的信息，它不参与序列身份。

**一条观测的关键字段：**

| 字段 | 含义 | 例子 |
|---|---|---|
| timestamp | 时间（UTC） | 2026-08-19 04:00:00 |
| source | 七类中的哪一类 | node |
| node_id | 归一后的官方网元 | chengdu-br-1 |
| metric | 统一指标名 | node.cpu_usage |
| value | 数值 | 0.694 |
| dimensions | 细分身份 | 空，或 `{interface_id: ens4}` |
| related_node_ids | 关联网元（只有 traffic 有） | (wuhan-service-vm-1, …) |
| direction | 异常方向 | high / low / both / state |
| event_role | 是否可开启事件 | trigger / support |

`(source, node_id, metric, dimensions)`就是序列的键，四项全同的两条观测属于同一条序列。

### 1.3 输入的维度

**没有张量化，没有统一的张量形状。** 输入是一堆长度不一的时间序列。

```text
索引 = (traffic, chengdu-traffic-vm, traffic.auth.success_ratio,
        {flow_type: auth, protocol: http, series_key: 4008dbc3…,
         source_region: chengdu, target_region: wuhan,
         target_domain: auth01.wuhan.aiops.local})

点列 = [(04:00, 1.0000), (04:01, 0.9962), (04:02, 0.9991), …]
```

关键在于：**观测上的「源、网元、指标、维度」四个字段不是特征，而是索引。** 它们决定这条观测属于哪条序列，本身不进入数值计算。所以每条序列的数值部分是单通道的（F = 1），序列之间的区别体现在索引上，而不是体现在特征向量里。

准确写法是 `输入 = { series_i : [ (t_1, v_1), …, (t_Ti, v_Ti) ] }`，第 i 条序列的长度 T_i 各不相同。

### 1.4 输出文件类型

正式预测文件是 JSONL，一行一条预测：

```json
{"prediction_id": "pred_000001", "start_time": "2026-08-19T07:01:00.000Z", "end_time": "2026-08-19T07:03:00.000Z", "root_cause_top5": [{"rank": 1, "network_element_id": "chengdu-traffic-vm"}, {"rank": 2, "network_element_id": "chengdu-service-vm-3"}, {"rank": 3, "network_element_id": "chengdu-fw"}, {"rank": 4, "network_element_id": "wuhan-service-vm-1"}, {"rank": 5, "network_element_id": "wuhan-service-vm-2"}], "fault_category": {"major_category": "service", "sub_category": "auth_error"}}
```

每个事件固定输出三部分：时间段（2 个时间戳）、根因数组（长度 5，rank 1–5，网元来自官方枚举且不重复）、分类（大类 + 子类）。

---

## 2. 流程与修改点

```text
七类 CSV
   ↓
run()                                  run.py
  ├─ 读配置   topology.json / network_elements / fault_taxonomy / model_v1.json
  │
  ├─ detect_events_streaming()         preprocessing/streaming.py
  │    ├─ iter_source_files()          发现七源 CSV
  │    ├─ iter_file_observations()     逐行解析    preprocessing/multisource.py
  │    ├─ CounterTransformer.transform() 语义转换  preprocessing/metric_semantics.py
  │    ├─ OnlineRobustDetector.add()   在线打分    anomaly_detector/streaming_detector.py
  │    └─ segment_evidence_by_city()   准入与切分  anomaly_detector/robust_detector.py
  │
  ├─ split_concurrent_events()         跨城拆分    anomaly_detector/event_clustering.py
  │
  └─ 对每个事件：
       ├─ rank_candidates()            localization/graph_fusion.py
       ├─ classify_event()             classification/prototype_model.py
       └─ _llm_event()                 可选 LLM 复核  run.py + models/
   ↓
predictions.jsonl
```

七类 CSV → five_sigma.detect() → 原始事件字典 → run.py::_legacy_events() → DetectedEvent → localization → classification

### 2.1 run.py 调用 preprocessing

`run()` 不自己读 CSV，把数据根目录交给 preprocessing，调用 `detect_events_streaming()`。这个函数内部按顺序做四件事：发现文件、解析成观测、过语义转换、送进检测器打分。

解析和语义转换紧挨着，检测器拿到的已经是统一语义的量，不需要知道某个字段原本是Counter还是Gauge。这是把解析从检测器里拆出来的直接原因：阈值可以按语义配置，而不是按列名硬编码。

### 2.2 preprocessing 输出什么结构

返回的 `StreamingDetectionResult` 含七项：

| 字段 | 内容 |
|---|---|
| events | `DetectedEvent` 元组，最终事件窗口 |
| diagnostics | 分钟能量、触发分钟、观测区间等诊断 |
| bundle | 观测集合（见下） |
| observation_count | 评测过的观测总数 |
| series_state_count | 序列数 |
| dropped_evidence_count | 因限额丢弃的证据数 |
| evidence | 切分前的全部证据点 |

其中 `bundle` 是 `ObservationBundle`，含四项：数值观测集合、文本观测集合（FRR 日志与采集错误）、逐源行数统计 `ParseStats`、每源读入行数。

### 2.3 检测器输出什么

分两级：先产出**证据点**，再由证据点组成**事件**。

```text
观测序列 [S]
   ↓ 在线滚动中位数/MAD 打分
证据点 AnomalyEvidence [N_evidence]
   ↓ 准入 → 分钟能量 → 城市内切分 → 峰值抑制
事件 DetectedEvent [N_event]
```

| 结构 | 是什么 | 关键字段 |
|---|---|---|
| `AnomalyEvidence` | 某序列在某一分钟偏离正常范围 | 时间、源、网元、指标、值、滚动中位数、偏离分数、事件角色、语义量程位置 |
| `DetectedEvent` | 证据点按时间聚成的窗口 | 起止时间、峰值时刻、置信度、证据列表、各源证据条数 |

`trigger` 可以开启事件，`support` 只能补充证据；未通过资格检查的trigger会被降级为 support。从证据点到事件有三道处理：

1. **准入**（`_qualified_trigger_evidence`）：trigger点要满足四条路径之一，状态证据、序列持续、跨族佐证、语义极值；support点不能自己开事件。
2. **聚合与切分**（`_energies` → `_windows_from_energy`）：按分钟聚合成能量，高低两个阈值迟滞切分（高开低维持），超30分钟的窗口从内部最低能量处劈开。
3. **峰值抑制**（`_suppress_nearby_events`）：用官方给的故障通常间隔≥20分钟先验，离太近的只保留质量最高的。

以上每座城市独立完成，证据在计算能量之前就按城市分开。

### 2.4 localization：拿到异常点后怎么排名

入口 `rank_candidates(event, network_config, topology, config)`，输入是一个事件加上官方网元列表、公开拓扑和权重配置。

**第一步，定候选范围。** 收敛到有证据依据的城市（直接证据城市、关联城市、拓扑边能扩展到的城市），避免零证据城市靠字典序进入 Top5。

**第二步，每个候选算 8 个特征加权求和：**

| 特征 | 权重 | 含义 |
|---|---|---|
| severity | 0.28 | 最强 3 条证据的强度（0.8×归一化分数 + 0.2×语义量程位置） |
| precedence | 0.16 | 时间领先性，越早异常越可能是根因 |
| directness | 0.16 | 直接性，本机资源/路由状态 1.0，traffic 0.35 |
| persistence | 0.15 | 去重到分钟后，异常分钟数占事件时长的比例 |
| source_diversity | 0.14 | 独立数据源数量 |
| topology_explanation | 0.08 | 该候选能在拓扑上解释多少其他异常节点 |
| relational_support | 0.07 | 来自 `related_node_ids` 的关联证据强度 |
| symptom_penalty | −0.04 | 只有关联证据、没有直接证据时的惩罚 |

目前的权重还有待优化。

**第三步，排序取前 5。** 所有特征对support证据降权（乘0.25）。

输出的 `RankingResult` 含 `top5`（固定 5 个提交候选）、`candidates`（全部候选及特征分量）、`by_node`（按网元索引）和 `scope`（候选范围与裁剪数量）。

### 2.5 classification：本地分类和 LLM Prompt

**本地分类**（`classification/prototype_model.py`）入口是 `classify_event(event, ranking, taxonomy, config)`。它把事件证据映射成稀疏的语义信号向量（cpu / load / memory / disk_io / bgp_down / service_error 等），按「分钟 × 信号」取最大值而不是累加（避免字段数量影响类别），再与官方 28 类故障原型做余弦相似度，减去反证原型、加上角色先验。返回大类子类、置信度、Top3 和信号明细。

**LLM 复核**只在启用 `transformers` 或 `api` 后端时触发。LLM 不读 CSV，`run.py::_hybrid_context()` 先把一个事件压成有界上下文：

| 上下文项 | 内容 |
|---|---|
| candidates | 最多 12 个候选，各带最多 6 条证据和 8 个特征分量 |
| topology | 只保留候选涉及的节点和边 |
| timeline | 最多 64 条时间线 |
| window | 事件起止时间 |
| prototype_hint | 本地分类器的结果，一并交给模型 |

套进四个模板（`baseline/bian/prompts/`），三段式调用：

| Prompt 文件 | 输入 | 输出 |
|---|---|---|
| `7b_a_device_analysis.txt` | 一批候选及其证据 | 每个设备的异常分析，12 词以内摘要 |
| `7b_b_stage1.txt` | 候选证据、设备分析、局部拓扑 | 全部候选的初筛分数 |
| `7b_b_stage2.txt` | 短名单、拓扑、时间线 | 候选 5 维评分，跑 3 轮按 Rank-of-Ranks 聚合 |
| `classification.txt` | Top5、事件上下文、合法分类表 | 一个大类/子类及置信度，跑 3 轮投票 |

LLM 返回值必须通过 JSON Schema 和官方枚举校验，失败时不会生成格式不合法的提交结果。后端两个：本地 Transformers（`models/backend.py`）和 OpenAI-compatible API（`models/api_backend.py`）。目前默认走本地分类；LLM 路径已实现，尚未接真实权重。
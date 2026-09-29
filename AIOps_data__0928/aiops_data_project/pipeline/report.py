"""Render the Markdown research report from pipeline artifacts."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List


def _read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_bytes(value) -> str:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.2f} {units[index]}"


def _table(headers: List[str], rows: Iterable[Iterable[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        cleaned = [str(item).replace("|", "\\|").replace("\n", " ") for item in row]
        lines.append("| " + " | ".join(cleaned) + " |")
    return "\n".join(lines)


def _short(text: str, length: int = 180) -> str:
    text = str(text or "")
    return text if len(text) <= length else text[: length - 1] + "…"


def render_report(project_dir: Path) -> Path:
    output_dir = project_dir / "outputs"
    inventory = _read_json(output_dir / "inventory.json", {})
    cases = _read_json(output_dir / "candidate_cases.json", [])
    official_examples = _read_json(output_dir / "official_case_attribution_examples.json", [])
    fields = _read_csv(output_dir / "field_dictionary.csv")
    mappings = _read_csv(output_dir / "source_mapping.csv")
    summary = inventory.get("source_summary", [])
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    source_rows = []
    for item in summary:
        source_rows.append([
            item.get("source", ""),
            item.get("origin", ""),
            item.get("grain", ""),
            _fmt_int(item.get("files")),
            _fmt_int(item.get("rows")),
            _fmt_int(item.get("columns")),
            item.get("storage_status", ""),
        ])

    mapping_rows = []
    for item in mappings:
        mapping_rows.append([
            item.get("source", ""),
            item.get("origin", ""),
            item.get("entity_fields", "").replace("|", ", "),
            item.get("primary_fault_families", "").replace("|", ", "),
            item.get("baseline_role", ""),
        ])

    case_rows = []
    for item in cases:
        root = (item.get("root_cause_top5") or [{}])[0]
        category = item.get("fault_category") or {}
        case_rows.append([
            item.get("case_id", ""),
            item.get("start_time", ""),
            item.get("end_time", ""),
            root.get("network_element_id", ""),
            f"{category.get('major_category', '')}/{category.get('sub_category', '')}",
            _short(item.get("root_cause_reason", ""), 120),
            _short(item.get("classification_reason", ""), 150),
        ])

    official_case_rows = []
    for item in official_examples:
        category = item.get("official_fault_category") or {}
        evidence_fields = ", ".join(evidence.get("field", "") for evidence in item.get("window_evidence", []))
        official_case_rows.append([
            item.get("official_case_id", ""),
            item.get("official_root_cause", ""),
            f"{category.get('major_category', '')}/{category.get('sub_category', '')}",
            evidence_fields,
            _short(item.get("why_root_cause", ""), 180),
            _short(item.get("why_category", ""), 180),
        ])

    field_rows = []
    for item in fields:
        field_rows.append([
            item.get("source", ""),
            item.get("field", ""),
            item.get("type", ""),
            item.get("unit", ""),
            item.get("meaning", ""),
            item.get("analytical_role", ""),
        ])

    report = f"""# 基于多源观测数据的骨干网故障诊断：数据工程与可解释归因报告

> 项目角色：数据负责人 @dyx  
> 生成时间：{generated}  
> 项目目录：`aiops_data_project/`  
> 研究性质：基于公开原始观测数据的可复现数据分析、官方 Case 归因解释与字段证据审计

![数据 pipeline 总览](figures/fig_01_pipeline_overview.png)

图 1. 从不可变原始层到可解释归因层的数据 pipeline。netflow 在高基数场景下先做时间窗口过滤和聚合，再进入证据融合。

## 1. 问题定义与数据边界

根据项目说明，本任务有两个输出：

1. 对每一个故障时间窗输出 Top-5 根因网元排序；
2. 将根因归入故障大类和子类。

公开 taxonomy 包含 5 个大类、28 个子类：

| 大类 | 子类 |
| --- | --- |
| link | delay, rate_limit, loss |
| firewall | acl_drop, rate_limit, port_block, cpu_pressure, default_route_error, rule_order_error |
| resource | cpu_pressure, memory_pressure, disk_io_pressure, disk_space_low, process_pressure, softirq_pressure |
| routing | blackhole, bgp_session_down, bgp_route_flap, wrong_static_route, ospf6_neighbor_down, ospf6_cost_anomaly, wrong_default_route |
| service | dns_down, dns_wrong_record, web_5xx, web_slow, auth_timeout, auth_error |

当前原始数据没有显式 `case_id`、`root_cause` 或 `fault_category` 字段，这是因为这些字段属于官方 Case / 评测标注层，而不是多源观测层。数据工程部分负责统计和规范化观测数据；归因部分使用官方已有 Case 的时间窗口，在窗口内解释为什么定位到官方根因网元、为什么归入官方故障分类，不自行重新定义 Case。

## 2. 原始数据组织与解压策略

原始压缩包及其城市目录均保留在项目根目录。每个城市目录沿用压缩包自带的层级：

```text
<city>_20260819040000_20260902040000/
└── <city>_20260819040000_20260902040000_data/
    ├── frr_syslog_events_*.csv
    ├── interface_metrics_*.csv
    ├── node_metrics_*.csv
    ├── routing_metrics_*.csv
    ├── scrape_health_*.csv
    └── traffic_flow_metrics.csv
```

已经解压 8 个城市的上述 6 类 CSV，共 48 个文件；原始压缩包未删除。netflow 没有整体展开，原因是 8 个城市明文体积约 96 GB。pipeline 通过 `--include-netflow` 提供显式开关，默认不复制和不展开这类文件。

## 3. 数据 inventory

### 3.1 数据源总体情况

{_table(["数据源", "源头/机制", "观测粒度", "文件数", "数据行数", "列数", "当前存储"], source_rows)}

![各数据源数据量](figures/fig_02_source_volume.png)

图 2. 已解压观测数据量。routing metrics 行数高，是因为它以长表形式保存多个路由指标和 label；行数大不代表信息维度一定更高。

### 3.2 Schema 维度

![各数据源字段数量](figures/fig_03_schema_dimensionality.png)

图 3. 各观测源字段数量。`traffic_flow_metrics` 的 126 列主要来自 DNS/Web/Auth/Elephant 四组重复指标，而不是 126 个完全独立的数据源。

### 3.3 关键数据质量规则

pipeline 对所有 CSV 采用流式读取，并统计以下质量项：

- 行数与列数；
- 首末观测时间；
- 列数不匹配的 malformed row；
- 缺失值比例；
- 实体字段基数及样例值；
- `NULL`、`\\N`、空字符串、`NA` 等缺失标记；
- 不同城市的同名源文件表头一致性。

时间处理遵循公开项目约定：无时区的时间字符串按 UTC 解释，再统一转换为带时区的 UTC 时间。`event_time` 用于事件归因，`received_at` 和 `inserted_at` 只用于传输/入库延迟分析，不能替代事件发生时间。

## 4. 多源数据源头与作用

{_table(["数据源", "推断源头", "实体键", "主要故障族", "在 pipeline 中的角色"], mapping_rows)}

![多源证据支持矩阵](figures/fig_04_evidence_matrix.png)

图 4. 规则化证据支持矩阵。数值表示该源对某一故障族的初始证据权重，不表示监督学习标签，也不代替 Case 内的时间先后和拓扑判断。

### 4.1 语义边界

不同数据源提供的是不同层级的证据：

```text
node_metrics       → 主机资源状态
interface_metrics  → 接口/链路状态
routing_metrics    → BGP、OSPF6、IPv6 路由控制面
scrape_health      → 监控采集质量
frr_syslog_events  → 路由软件离散事件
traffic_flow       → DNS、Web、Auth 服务体验
netflow            → 五元组与端口级流量事实
```

这些源不能互相替代。例如：

- `scrape_up=0` 表示观测中断，不自动等于设备故障；
- 服务错误率升高表示用户侧症状，不自动等于服务进程是根因；
- 接口异常可以是路由或资源故障的后果，不一定是链路根因；
- FRR 日志需要和 routing metrics 的状态变化对齐，不能只按日志数量排序。

## 5. 字段字典

字段字典由 `pipeline/inventory.py` 根据真实表头生成，同时使用 `pipeline/definitions.py` 中可审阅的语义映射。完整机器可读版本位于：

- `outputs/field_dictionary.csv`
- `outputs/field_dictionary.json`
- `outputs/schema_reference.csv`（包含未展开 netflow 的契约字段，标记为 `definition_reference_only`）

字段字典的每一行包含：字段所属源、原始字段名、数据类型、单位、语义、分析作用和原始缺失值标记。

{_table(["源", "字段", "类型", "单位", "含义", "分析作用"], field_rows)}

对于长表和宽表，需要特别注意：

1. `routing_metrics` 的语义由 `metric_name + label + value` 共同决定；
2. `traffic_flow_metrics` 的 `*_total`、`*_sum`、`*_count` 是累计量，pipeline 不直接对累计量做五阶异常检测，而应先做差分或窗口速率；
3. `netflow` 的 `node_key`、`node`、`interface_id` 和地址端口共同描述一条流，不能只按 node 聚合后丢失五元组信息；
4. `NULL` 和 `\\N` 都应在规范化层映射为缺失，但原始值必须在审计层保留。

## 6. Pipeline 设计

### 6.1 处理阶段

```text
P0  原始层登记
    压缩包、分卷、城市目录、文件大小

P1  数据 inventory
    表头、行数、时间范围、缺失值、实体基数、质量问题

P2  规范化
    UTF-8 BOM、UTC、缺失标记、数字类型、node/interface/hostname 映射

P3  官方 Case 对齐
    官方 Case 时间窗 → 8 个城市 × 多源观测窗口

P4  证据融合
    局部异常、时间先后、多源一致性、参考拓扑、症状传播

P5  输出
    Top-5 根因、fault_category、解释文本、机器可读 JSONL/CSV
```

### 6.2 官方 Case 的处理单位

一个官方 Case 不是由本项目重新切出的异常窗口，而是官方给定的全网时间窗口：

```text
OfficialCase_i = [official_start_time, official_end_time]
                × 8 个城市
                × 全部观测源
                × 全部候选网元
```

窗口内必须同时提取 8 个城市的数据。只查看官方根因所在城市会丢失传播证据，也无法解释为什么其他网元没有被定位为根因。

### 6.3 官方 Case 归因数据流

1. 读取官方 Case 的 `case_id`、`start_time` 和 `end_time`；
2. 按时间窗口从所有城市和观测源提取数据；
3. 依据公开网络元素清单建立完整候选网元集合；
4. 为每个候选网元保留窗口内的局部异常证据、时间先后和数据源；
5. 结合参考拓扑判断根因和下游症状；
6. 用字段机制和官方 taxonomy 解释大类、子类；
7. 将官方答案与证据解释并列保存，不把官方标签当作输入特征。

项目仍提供 `--official-baseline-only` 用于复现公开 five-sigma 的窗口检测规则，但它是 baseline 的候选窗口组件，不是替代官方 Case 清单的 Case 构造器。由于当前目录是连续约 14 天的原始包，直接全量运行该 detector 会得到一个超长兼容性窗口；这个结果只用于核对 baseline 接口，不能替代官方 Case。

## 7. 官方 Case 的根因定位与故障分类解释

### 7.1 为什么定位到这个网元

对官方 Case 的根因解释必须同时检查：

1. 该网元是否在官方窗口内出现与故障机制直接对应的字段异常；
2. 该异常是否早于其他网元的传播性症状；
3. 异常是否集中在该网元，而不是全网采集失败或共同的观测伪影；
4. 参考拓扑是否支持“该网元影响其他网元”的传播关系；
5. 是否存在更早、更直接的资源、接口或控制面证据可以替代当前候选。

因此，根因定位不是“异常值最大的设备”，而是“时间、机制、局部性和传播关系共同最符合的设备”。

### 7.2 为什么分类成这个故障

故障分类首先依据字段所表达的故障机制，再判断子类：

| 证据族 | 典型字段/事件 | 分类解释 |
| --- | --- | --- |
| 资源 | CPU、load、memory、disk IO、filesystem、process | 说明主机资源压力，再由具体字段区分 CPU、内存、磁盘、进程等子类 |
| 链路 | drop、error、carrier、loss、latency、jitter | 说明接口或链路质量问题，不能只凭字节/包速率升高分类 |
| 路由 | BGP peer、prefix、OSPF6 neighbor、route change、FRR log | 说明控制面状态或路由协议故障 |
| 服务 | DNS/Web/Auth 延迟、QPS、失败、超时 | 说明用户侧服务异常，需要排除更早的资源或网络根因 |
| 防火墙 | firewall 网元、端口选择性异常、丢弃模式 | 说明策略、规则、端口或防火墙资源导致的流量选择性失败 |

分类说明必须同时写出反证。例如，`disk_io_pressure` 需要说明磁盘利用率和读写速率直接饱和，而 CPU/load 升高可能只是 I/O 等待的伴随症状；`bgp_session_down` 需要说明 peer 状态和前缀接收变化，而不是只看到业务流量下降。

### 7.3 公开 baseline 样例 Case 结果

下表使用公开 baseline 的官方样例 Case，展示“已有 Case → 窗口证据 → 定位原因 → 分类原因”的写法：

{_table(["官方 Case", "根因网元", "官方分类", "关键字段", "为什么定位", "为什么分类"], official_case_rows)}

逐 Case 的窗口统计、反证和解释字段见：[official_case_attribution_examples.json](outputs/official_case_attribution_examples.json)。

`candidate_cases.json` 仍然保留，但只用于连续原始数据的探索性异常审计，不能替代官方 Case 或官方答案。

## 9. 运行方式

在项目根目录执行：

```bash
python3 aiops_data_project/run_pipeline.py
```

默认行为：

- 扫描已解压 CSV；
- 生成 inventory、字段字典和源头映射；
- 不展开、不读取未落盘的 netflow；
- 不从连续原始数据构造官方 Case；
- 生成 PDF/PNG 图表；
- 生成本报告。

如需生成连续原始数据上的探索性异常审计结果，必须显式运行：

```bash
python3 aiops_data_project/run_pipeline.py --exploratory-cases
```

如果后续磁盘空间足够并且已经把 netflow 放入对应城市 `_data` 目录，可以显式运行：

```bash
python3 aiops_data_project/run_pipeline.py --include-netflow
```

只重新生成图和报告：

```bash
python3 aiops_data_project/run_pipeline.py --report-only
```

## 10. 局限与下一步

当前项目已经完成数据负责人部分的第一版闭环，但以下内容仍然需要和模型负责人共同确认：

1. 真实比赛 Case 的数量和隐藏故障窗口；
2. `monitor-vm` 等没有直接观测记录的候选网元如何进入模型输入；
3. 当前公开参考拓扑与真实网络拓扑之间的差异；
4. netflow 的窗口聚合粒度和是否需要加入 baseline；
5. 累计计数器的差分特征；
6. firewall 与 link/routing/service 的判别优先级；
7. 候选 Case 与提交结果的时间边界校准。

因此最终交付不应只有一个预测文件，而应同时保留：

```text
预测结果
+ Case 时间窗
+ Top-5 候选
+ 关键证据
+ 分类依据
+ 反证与不确定性
```

这能保证模型结果可复盘，也能在评测分数不理想时定位问题究竟来自异常检测、时间窗、根因排序还是故障分类。

## 附录：项目文件

| 文件 | 作用 |
| --- | --- |
| `run_pipeline.py` | 一键运行 pipeline、图表和报告 |
| `pipeline/definitions.py` | 数据源、字段、taxonomy、候选网元和证据规则 |
| `pipeline/inventory.py` | 流式 inventory、字段字典和源头映射 |
| `pipeline/cases.py` | 连续原始数据的探索性异常审计，不替代官方 Case |
| `figures/generate_figures.py` | 学术级 PNG/PDF 图表生成 |
| `outputs/inventory.json` | 机器可读数据总览 |
| `outputs/field_dictionary.csv` | 完整字段字典 |
| `outputs/schema_reference.csv` | 未展开源的契约字段定义与状态标记 |
| `outputs/source_mapping.csv` | 多源数据来源与诊断作用 |
| `outputs/candidate_cases.jsonl` | 逐 Case 机器可读归因结果 |
| `outputs/official_baseline_events.jsonl` | 公开 five-sigma 规则生成的候选时间窗（可选） |
| `outputs/official_case_attribution_examples.json` | 公开 baseline 官方样例 Case 的窗口证据与归因解释 |

## 参考资料

- [项目说明](../AIOPS_OnePage.md)
- [数据集 README](../README.md)
- [公开 baseline](https://gitee.com/murina/aiops-challenge-2026)
- [提交格式与 taxonomy](https://gitee.com/lin-xianghong/aiops-challenge2026-submission)
"""
    report_path = project_dir / "AIOps_Data_Academic_Report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path

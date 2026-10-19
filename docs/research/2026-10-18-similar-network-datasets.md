# 与项目初始数据相似的数据集及论文检索

检索日期：2026-10-18。范围：IP/骨干网络的多源监控、异常检测、故障定位与根因分类。

## 结论

有采用相似数据的论文，但本次未检索到明确使用本项目 **CCF AIOps 2026 同一批原始 CSV** 的公开论文。这是本次检索的覆盖结论，不代表证明此类论文不存在。

- **字段相似、适合复现检测：** Cisco Telemetry 对应的 BGP stream-learning（2018）与 ADT（2023）。
- **故障范围与任务相似、适合研究诊断：** NIKA（2025 预印本）及采用 NIKA 的 SADE（2026 预印本）。
- **生产网络多源证据到设备排序相似：** BiAn（SIGCOMM 2025）；本次未确认其原始生产数据有公开下载入口。
- **有明确公开数据、便于建立传统基线：** SOFI 数据论文（2022）及采用 SOFI 的增量学习论文（2026）。
- **对应项目 NetFlow 支路：** NLOF（2020），但需要可靠的流到路径/链路映射。

这里的相似度是依据观测字段、网络对象及标签任务作出的定性判断，不是论文给出的跨数据集相似度分数。

## 1. 本项目原始数据画像

核对依据：[需求摘要](../requirements/AIOPS_OnePage.md)、[原始字段清单](../../data_side/results/raw_schema.csv)、`sample/case_001/` 下实际 CSV 表头，以及 [v4 架构报告](../v4-architecture-report-2026-10-18.md)。没有以模型后生成的特征代替原始数据。

| 原始来源 | 已核对内容 | 对照文献时关注的问题 |
|---|---|---|
| node | CPU、load、可用内存、swap、磁盘读写、文件系统、进程数等 | 设备资源异常是否作为网络故障证据 |
| interface | 收发字节/包速率、丢弃、错误、carrier changes、接口身份 | 接口抖动、链路退化与异常传播 |
| routing | `metric_name/label/value`，包含 BGP/OSPFv3/路由相关观测 | 路由邻居、协议状态与控制平面故障 |
| scrape | exporter 身份、采集成功、时延、样本数和错误 | 采集缺失与设备故障能否区分 |
| traffic | DNS、Web、认证及 elephant flow 的时延、成功/失败、吞吐等 | 主动业务观测与底层故障能否关联 |
| netflow | 分钟五元组、观测设备/接口、packets、bytes、首末时间 | 受影响流与故障设备/链路的关系 |
| frr | 时间、hostname、program、severity、message 等日志字段 | 协议事件与数值指标能否对齐 |

需注意：七类文件不代表每个案例中七源都存在有效记录；实际抽查的一个 FRR 文件只有表头。项目报告也记录第二、三批没有 traffic 与 frr。因此，下面将“覆盖部分来源”与“全栈数据等价”分开。

输出任务包括事件时间、根因网元 Top-5、故障大类/子类。项目分类配置包含 link、firewall、resource、routing、service 五大类。官方赛事介绍确认该赛题依托 FITI 平台：[赛事官网](https://competition.aiops.cn/)。

## 2. 核心论文与数据对应

### 2.1 Cisco 遥测：与接口和路由字段最直接对应

**Andrian Putina et al. Telemetry-based stream-learning of BGP anomalies. Big-DAMA ’18, pp. 15–20, 2018.**

- DOI：[10.1145/3229607.3229611](https://doi.org/10.1145/3229607.3229611)。注意这是 SIGCOMM 期间的 Big-DAMA workshop 论文。
- [作者论文 PDF](https://nonsns.github.io/paper/rossi18bigdama.pdf)；[Cisco 原始数据仓库](https://github.com/cisco-ie/telemetry)；[复现代码](https://github.com/anrputina/OutlierDenStream-BigDama18)。
- 仓库明确提供 CSV、字段定义、事件时间及触发设备记录，并提供拓扑资料。实验涵盖 BGP Clear、Port Flap、端口关闭及收发器拔插等。
- **项目对应：** `routing` 与 `interface` 两类时序，以及异常发生时间、设备维度。
- **可借鉴：** DenStream 在线聚类与状态变化检测，作为现有规则检测器的独立比较方法。
- **差异：** 覆盖的是路由/接口事件，不能据此声称支持本项目全部防火墙和业务故障；不是完整的七源联合诊断基准。

**Parisa Foroughi, Frank Brockners, Jean-Louis Rougier. ADT: AI-Driven network Telemetry processing on routers. Computer Networks 220, 109474, 2023.**

- DOI：[10.1016/j.comnet.2022.109474](https://doi.org/10.1016/j.comnet.2022.109474)；[出版社页面](https://www.sciencedirect.com/science/article/pii/S1389128622005084)。正式卷期为 2023，不能仅按 DOI 中的 2022 判定年份。
- 论文描述路由器操作计数器，包括接口流量、BGP、BFD、CPU 和基础设施统计，以约 10 秒周期采集。
- **项目对应：** `node + interface + routing`，属于本次字段层面最接近的文献之一。
- **可借鉴：** DESTIN 以主角度衡量多变量时序窗口的变化，适合比较“状态发现→候选事件”的检测部分。
- **差异与可用性：** 论文声明评估数据公开；本次没有完成其全部实验数据链接及版本的一一核对，不把上面的 2018 Cisco 仓库直接等同于 ADT 全部数据。检测状态变化也不等于完成网元 Top-5 和故障子类分类。

### 2.2 NIKA 与 SADE：网络对象、故障语义与诊断目标相近

**Zhihao Wang et al. A Network Arena for Benchmarking AI Agents on Network Troubleshooting. arXiv:2512.16381, 2025.**

- [论文](https://arxiv.org/abs/2512.16381)；[原始版本全文](https://arxiv.org/html/2512.16381v1)；[框架](https://github.com/sands-lab/nika)；[公开轨迹数据](https://zenodo.org/records/17971675)。本次按预印本标注，没有推定正式会议录用。
- 初始论文版本包含 54 种网络问题、五类网络场景；包括链路抖动、BGP ASN 错误、接口微突发、服务 DoS 等。数据与工具涉及端口计数器、路由表、事件日志及主动测量。
- **项目对应：** 设备/接口/路由/资源/业务多层证据；故障检测、定位和根因判断。
- **重要差异：** 核心是可交互的仿真环境与工具调用；公开 Zenodo 包主要是 906 条诊断轨迹及真值/提交。它不是可直接替换项目 CSV 的连续离线遥测表。
- **版本边界：** 当前仓库已扩充场景和任务；本文的 54 种问题等数字以 2025 初始论文/数据发布为准，不混用当前仓库数字。

**Kuan-Hao Tseng et al. SADE: Symptom-Aware Diagnostic Escalation for LLM-Based Network Troubleshooting. arXiv:2605.04530, 2026.**

- [论文及作者摘要](https://arxiv.org/abs/2605.04530)。本次按预印本标注。
- **明确采用的数据/环境：** NIKA。论文报告在 523 个留出故障事件、11 个未见场景上评估。
- **可借鉴：** 分阶段采证、再形成诊断判断，并按故障家族组织排查。与本项目的证据索引和 LLM 诊断阶段有直接比较价值。
- **差异：** 在线 Agent 能请求新的观测；本项目通常只能查询已提供的观测。其性能不能直接套用到本项目。

### 2.3 BiAn：生产网络的多源监控与故障设备排序

**Chenxu Wang et al. Towards LLM-Based Failure Localization in Production-Scale Networks. ACM SIGCOMM 2025.**

- DOI：[10.1145/3718958.3750505](https://doi.org/10.1145/3718958.3750505)；[作者公开全文](https://cs.nju.edu.cn/tianchen/lunwen/2025/sigcomm25-chenxu.pdf)。
- BiAn 在阿里云生产网络中使用监控证据生成故障设备排序和解释，并在诊断中利用拓扑与时间线。论文基于 17 个月真实案例评估。
- **项目对应：** 从多设备、多源异常证据识别根因网元，尤其适合对照当前“候选生成→证据组织→LLM 排序”的设计。
- **可借鉴：** 设备级证据压缩、拓扑和时序关联、分层推理。
- **差异与可用性：** 本次未确认有可公开下载的原始生产数据；不能当成可直接用于外部训练/复现的数据集。论文定位任务也不等于本项目全部故障子类识别。

### 2.4 SOFI：公开的 IP 网络症状—故障数据

**Angela Vargas-Arcila et al. SOFI dataset: Symptom-fault relationship for IP-network. Computer Networks 216, 109233, 2022.**

- DOI：[10.1016/j.comnet.2022.109233](https://doi.org/10.1016/j.comnet.2022.109233)；[出版社说明](https://www.sciencedirect.com/science/article/pii/S1389128622003127)；[Mendeley 数据 v2](https://data.mendeley.com/datasets/tc6ysmh5j8/2)。
- 仿真企业 IP 网络的周期监控，包含人工注入链路、配置和协议问题；约 649 小时监控，其中约 10 小时处于注入故障期间。
- 数据页明确给出 34 个属性、12,971 个实例，处理版类别为 F（故障）与 NE（正常）。
- **项目对应：** 网络性能观测、接口症状、正常/异常不平衡与故障传播。
- **差异：** 公开处理版标签主要是二分类，不能直接作为项目网元 Top-5 或细粒度故障类别的监督标签。

**Angela María Vargas-Arcila et al. Fault Diagnosis for IP-Based Networks Using Incremental Learning Algorithms and Data Stream Methods. Technologies 14(2), 132, 2026.**

- DOI：[10.3390/technologies14020132](https://doi.org/10.3390/technologies14020132)；[全文](https://www.mdpi.com/2227-7080/14/2/132)。
- **明确采用的数据：** SOFI。比较 25 种增量学习算法，并结合 ADWIN 漂移检测和 SMOTE 重平衡。
- **项目对应：** 多批次分布变化、正常样本占多数、连续监测的异常检测基线。
- **边界：** 这是对检测阶段的参考；不能据其二分类实验推导出对全部故障类型的根因定位能力。

### 2.5 NLOF：与 NetFlow 五元组数据支路对应

**Christopher Mendoza, Michael P. McGarry. The Network Link Outlier Factor (NLOF) for Fault Localization. IEEE Open Journal of the Communications Society 1, 1539–1550, 2020.**

- DOI：[10.1109/OJCOMS.2020.3025663](https://doi.org/10.1109/OJCOMS.2020.3025663)；[IEEE 论文页](https://ieeexplore.ieee.org/document/9204754)。
- 输入是流记录（例如 NetFlow）和网络拓扑。通过流吞吐聚类、异常流评分、流到链路的关联，计算链路故障分数；实验采用 Mininet。
- **项目对应：** `netflow` 中的 packets/bytes、时间、设备/接口维度；适合探索异常流是否汇聚到某个根因链路。
- **落地条件：** 必须有可靠路径或链路关联。项目现有观测接口标识本身不足以确定端到端路径；不能仅凭五元组和接口名直接套用完整算法。
- **数据可用性：** 本次核对到论文的实验数据类型与方法，未确认独立实验数据包的公开下载入口。

## 3. 补充线索与容易误判的论文

**Tikumporn Wankvar, Apichon Witayangkurn. Automated Root Cause Analysis of Network Failures in IP/MPLS Network Using Machine Learning and Case-Based Reasoning. IEEE Access 13, 111542–111554, 2025.**

- [DOI](https://doi.org/10.1109/ACCESS.2025.3583817)；[出版社摘要](https://ieeexplore.ieee.org/abstract/document/11053841)。
- 使用 SNMP Trap、Syslog 及诊断流程，摘要报告超过七百万条事件记录，网络对象与多源事件处理有参考意义。本次只确认摘要和元数据，没有确认其数据公开状态或与项目数值表逐字段的对应关系，列为补充。

**NetRCA: An Effective Network Fault Cause Localization Algorithm（ICASSP 2022）**

- [原文](https://arxiv.org/html/2202.11269v1)；DOI：[10.1109/ICASSP43922.2022.9747882](https://doi.org/10.1109/ICASSP43922.2022.9747882)。
- 虽然题目含 Network、AIOps、Root Cause，但实际是无线/5G KPI 与给定因果图，包含 2,984 个样本、23 个可观测变量，约 45% 有标签。
- 可借鉴少标签学习、时序特征和图推理；**数据相似性低于 Cisco 遥测和 NIKA**，不能因为同属 AIOps 挑战赛就当成同一类骨干网数据。

**NetOpsBench（2026 软件基准）**

- [官方仓库](https://github.com/NetX-lab/NetOpsBench)；[公开轨迹](https://huggingface.co/datasets/yyyyyt/netopsbench-trace)。
- 提供 SONiC/Containerlab 环境，BGP、gNMI、Syslog、Pingmesh 等观测与故障真值，网络对象相近。但仓库给出的引用类型是 software；本次没有确认配套学术论文，故不计入核心论文。

## 4. 面向本项目的阅读与使用顺序

| 目的 | 优先阅读 | 实际用途 |
|---|---|---|
| 改进当前根因候选排序与解释 | BiAn → SADE | 证据组织、时间/拓扑关系、诊断阶段约束 |
| 找字段相近且有公开数据的检测对照 | Cisco BGP stream-learning → ADT | 路由/接口多变量检测，比较规则与状态变化方法 |
| 构建覆盖更多网络故障的外部验证 | NIKA | 受控故障注入、检测/定位/根因分开评估；需另做离线观测适配 |
| 做传统在线学习与漂移基线 | SOFI + 2026 增量学习论文 | 仅在其标签支持的正常/故障任务上比较 |
| 增强 NetFlow 对定位的贡献 | NLOF | 在补齐路径关联后做链路候选打分 |

结合本项目原始输入与现有架构，优先精读 **BiAn、Cisco 遥测论文、NIKA/SADE**，分别回答“怎样组织证据并排序”“怎样从数值观测发现变化”“怎样设计网络故障诊断与评估”。

## 5. 检索范围与核对记录

- 检索入口：公开网页学术检索、SciSpace；元数据用 Crossref DOI 记录交叉核对，关键数据描述使用论文原文、作者仓库、出版社和数据存储库。
- 检索词覆盖 `network fault diagnosis dataset BGP OSPF metrics logs`、`backbone network syslog NetFlow root cause`、`network telemetry dataset Cisco`、`NIKA troubleshooting`、赛事完整中文名称、原始文件名 `frr_syslog_events`、FITI 与故障诊断组合。
- 按 DOI/标题合并预印本与正式版本；例如 SciSpace 返回的 NetRCA 正式版与 arXiv 版视为同一项研究。
- 本环境未挂载技能所列的 academic-search MCP。尝试技能附带的 OpenAlex 发现脚本时返回 HTTP 429；随后使用网页、SciSpace 和 Crossref 完成核查。
- 部分出版社全文打开失败，仅使用可访问的摘要/片段与其他第一方来源；没有把未阅读全文的补充候选表述为已经全文精读。
- 本次没有下载大型实验数据、运行外部模型或验证跨数据集迁移效果。公开数据入口已核查页面，完整数据包的下载与许可细项仍需在实际使用时逐项检查。

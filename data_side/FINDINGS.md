# 八城数据侧审计结论

**输入版本**：`data/feature_store/v2/stage1_all_cities_v2_20260926`；当前直接检测于 2026-09-27 完成门槛对齐后重跑，详见 [summary.json](results/summary.json) 的 manifest SHA-256。时间范围为 2026-08-19 04:00 至 2026-09-02 03:59 UTC，共 20,160 分钟。门槛变更及同版本对照见 [推理门槛对齐](THRESHOLD_ALIGNMENT.md)。

公开三例单独回归：TP 3、FP 0、FN 0、Total 96.011111，摘要保存在 [门槛运行对照](results/threshold_comparison.json)。它只校验现有样例，不外推到无标签八城数据。

## 已核实的数据事实

1. **文件与解析覆盖**：八城各有七源，共 56 个 CSV；原始表头在城市间没有漂移。本轮独立逐行扫描全部约 96 GiB（103.2 GB）原始文件，合计 521,491,569 行，按文件与既有特征构建审计完全一致，未发现列数错误的 CSV 记录；得到 [207 个源内字段的缺失统计](results/raw_field_quality_by_source.csv)。构建审计的 `bad_rows`、非法数值、无法归一实体和乱序行计数均为 0，manifest 质量状态为 `clean`。这里的“非法数值为 0”不等于“缺失为 0”：两种审计计数对象不同。另完整扫描 node 原始行得到 [72 个设备别名映射](results/raw_node_aliases.csv)。
2. **监控网元缺观测**：官方 80 个合法网元中，八个 `*-monitor-vm` 在 node/interface/routing/scrape/netflow/traffic/frr 特征中均无观测分钟；同名网元也没有保留的边。若第一批故障可能注入这些设备，单靠现有特征无法直接定位，需要确认导出范围或注入范围。逐网元证据见 [entity_source_coverage.csv](results/entity_source_coverage.csv)。
3. **不同城市原始行数差异主要来自 NetFlow**：全源总行数从成都的 39,777,374 到上海的 128,764,404；NetFlow 单源分别约 3,296 万和 1.216 亿。其他源的行数远更接近。不能仅据此判断丢数或故障多少，应由采集部署和流量规模解释。逐文件数据见 [file_inventory.csv](results/file_inventory.csv)。
4. **业务目标是固定区域的服务组**：当前特征中 DNS 请求目标为 beida、Auth 为 wuhan、Web 为 shanghai，Elephant 涉及八城。已列出 24 个目标域名，但域名到具体 `service-vm` 的权威对应不在已给配置内，见 [service_domain_mapping_worklist.csv](results/service_domain_mapping_worklist.csv)。
5. **接口与物理链路映射未给出**：特征中有 482 个网元接口组合和 NetFlow 接口边，但 `interface_id` 本身不能证明对端设备。需要填充 [interface_link_mapping_worklist.csv](results/interface_link_mapping_worklist.csv) 的实际对端与证据来源。
6. **缺失具有明确结构**：NetFlow 的 `if_role` 在 465,667,376 行中全部缺失；接口源的 `if_role` 缺失 7,455,206/9,712,086 行。路由 `value` 缺失 1,935,264/42,139,971 行，主要集中在 CR 角色的 BGP 指标，例如 `bgp_peer_up` 的 CR 322,544 行全部缺失，而 BR 1,046,701 行无缺失。不可把 CR 缺失值填成“BGP down”。业务流宽表应按 `flow_type` 计算条件缺失：DNS/Web/Auth 的各自指标列在其活动流行中完整；Elephant 的平均时延、p95 时延和 QPS 在全部 6,301 条活动行中缺失，丢包、抖动、吞吐率各缺失 912 行。细目见 [路由分组表](results/routing_metric_role_quality.csv) 和 [业务流分组表](results/traffic_flow_field_quality_by_flow.csv)。
7. **Node 的少量缺失可逐行定位**：22 条 BR 节点记录出现分组式部分缺失，其中 11 条缺 CPU 与三项磁盘指标，另外 11 条缺负载、内存、文件系统和进程等八项指标；其余指标仍在同一行，不能把整行删掉。清单见 [node_partial_missing_rows.csv](results/node_partial_missing_rows.csv)。
8. **量程需要按字段分别核准**：既有 DYX 字典把 `cpu_usage` 与 `disk_io_util` 写成 `ratio`，但当前特征观测到的最大值分别为 100 和 297.493；后者在 9 个网元上有 [47 个分钟单元超过 100](results/disk_io_above_100.csv)。两列不能按 0–1 比例解释，`disk_io_util` 超 100 的原始计算或聚合含义需要数据提供方核准。[量程审计](results/unit_scale_audit.csv) 给出七个相关字段的全部观测范围。

逐源原始行数（本轮全量扫描与构建审计一致）：

| node | interface | routing | scrape | traffic | netflow | frr |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,450,970 | 9,712,086 | 42,139,971 | 2,096,458 | 421,227 | 465,667,376 | 3,481 |

## 无标签候选审计，只用于复核排序

当前直接检测生成 **403 个候选事件**，其中 beida 156、shanghai 90、wuhan 74、xian 23、chengdu 20、guangzhou 18、nanjing 14、shenyang 8。`resource.cpu_pressure` 为 103 条。证据审计把 201 条已定义类别锚点列为 supported、0 条列为 unsupported；另 202 条由于相应类别尚无审计锚点而未评估，不能视为已验证正确。另有 16 条峰值驱动源并列难分，在 [candidate_review.csv](results/candidate_review.csv) 标为 `high`，仅表示**优先人工核查**，不是误报标记。

审计还报告 105 条候选的 Top1 根因城市与事件城市不一致，其中没有违反已启用的城市硬限制。这可能包含跨城影响，也可能反映定位偏差；需要真实标签和拓扑映射才能判断。候选总数、各城比例、类别分布都不应拿来对齐官方公开故障总量。

## 可用于后续标定的统计底座

[metric_profile.csv](results/metric_profile.csv) 记录 397 个城市/模型指标组合的观测值和相邻分钟变化分位数；[dimension_series_delta.csv](results/dimension_series_delta.csv) 汇总 11,006 条有维度序列，形成 1,168 个城市/源/指标组合。举例：`node.cpu_usage` 的相邻分钟绝对变化 p99 在 beida 为 5.995，在 guangzhou 为 2.689。两城数值不同，但当前统计**包含未知故障时段**，不能直接设成城市阈值。拿到确认正常窗口后，应在相同实体、相同指标、相同接口/peer 维度内重算，再用留出城市和时段验证。

## 仍需外部事实

按 [LABEL_CONTRACT.md](LABEL_CONTRACT.md) 提供第一批故障真值、正常窗口、接口对端、域名归属和 monitor-vm 采集/注入说明。没有这些信息，不能计算八城 TP/FP/FN、根因 Top-5 准确率、分类准确率，也不能宣称 403 个候选中哪些是真的故障。

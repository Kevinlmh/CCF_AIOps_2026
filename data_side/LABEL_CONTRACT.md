# 第一批数据的标注和映射交付契约

本文件明确仍需由数据提供方或有权限的标注者确认的事实。`results/candidate_review.csv` 是模型输出，可用于查证定位，**不能转写成真实标签**。

## 1. 真实故障：`stage1_ground_truth.jsonl`

一行一个独立故障，严格使用仓库官方评测器的字段：

```json
{"ground_truth_id":"由标注方给出的唯一ID","start_time":"2026-08-19T04:00:00Z","end_time":"2026-08-19T04:05:00Z","root_cause":{"network_element_id":"beida-br-1"},"fault_category":{"major_category":"routing","sub_category":"bgp_session_down"}}
```

上面只是**格式示例，不是第一批真实故障**。时间必须带 UTC 时区，`end_time` 晚于 `start_time`；根因 ID 必须在官方 80 个网元内，大类/小类组合必须在官方 28 类中；故障 ID 不重复。一个故障如存在多处独立根因或时间边界不确定，应另附 `annotation_notes.csv` 说明，不要通过增加未经核实的“故障”凑数量。文件应明确覆盖的是全部八城 14 天，还是某些经过完整复核的窗口。

收到后先运行官方格式校验，再用官方评测器逐事件对比：

```bash
.venv/bin/python -m aiops_challenge_2026.evaluator \
  --ground-truth data_side/handoff/stage1_ground_truth.jsonl \
  --predictions data_side/runs/stage1_direct_calibrated_20260927/predictions.jsonl \
  --report data_side/handoff/stage1_evaluation.json
```

**不要用仅部分窗口的标签评测全时段预测**，否则未标注窗口中的候选会被错误算作 FP。若只交付部分标签，需同时提供完整已复核时间窗口，并把预测裁剪到这些窗口后评估。

## 2. 确认正常窗口：`confirmed_normal_windows.csv`

字段：`window_id,city,start_time,end_time,reviewer,review_basis`。一行一个已完整核查、无本赛题故障的时间段；`city` 使用八个官方城市之一，跨城正常窗口请分别记录。需说明是否查过设备告警、注入日志与业务症状。用于从 [指标分布](results/metric_profile.csv) 中筛出真正可标定的背景，而不是假定未预测分钟都正常。

## 3. 物理和服务映射

- 接口链路：以 [接口工作清单](results/interface_link_mapping_worklist.csv) 为底，提供 `node_id,interface_id,peer_node_id,peer_interface_id,valid_from,valid_to,mapping_evidence`；无法确认的接口保持空值，不猜测。
- 服务域名：以 [域名工作清单](results/service_domain_mapping_worklist.csv) 为底，提供 `target_domain,target_region,network_element_id,valid_from,valid_to,mapping_evidence`。若一域名对应多个服务网元，可多行表达；不要默认平均分配到同城三个 service-vm。
- 原始主机名别名：如数据导出中存在多种原始标识，提供 `source,raw_identifier,network_element_id,valid_from,valid_to,mapping_evidence`，便于独立复核解析器归一结果。
- 请确认八个 `monitor-vm` 在第一批数据中是否应该有观测，是否可能作为故障根因；若没有观测而仍可注入故障，提供可以间接定位它们的有证据拓扑关系。
- 字段量程与采集约定：请确认 `cpu_usage`、`disk_io_util` 的原始单位和聚合方法，特别是 `disk_io_util > 100` 的解释；说明 `if_role` 缺失、CR 的 BGP 指标空值，以及 Elephant 时延/QPS 空值属于未采集、不适用还是导出缺陷。提供原始计数器重置和采样间隔约定，以便检验差分规则。

## 4. 接收验收

交付时记录数据版本、覆盖城市与时间、标注者、标注依据和未知/争议案例。先用官方 `validate_ground_truth` 检查格式与枚举，再检查故障时间是否落在声明的完整标注窗口内、正常窗口是否与故障重叠，最后才进行阈值选择。阈值拟合与最终评估必须按时间或城市隔离，不能在同一批故障上同时选参数和报告效果。

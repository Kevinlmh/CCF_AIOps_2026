# 2026 AIOps 赛题数据侧工作包

范围：第一批八城 14 天原始数据、当前 v2 特征库和 2026-09-27 门槛对齐后代码的直接检测候选。所有统计都是**无标签观测统计**，不能直接解释为正常分布、误报率或真实故障数量。

## 已完成的交付

| 文件 | 用途 |
| --- | --- |
| [一键数据侧 pipeline](run_pipeline.py) | 重建逐源审计、报告、字段字典、公开 Case 证据并检查结果一致性 |
| [数据质量结论](FINDINGS.md) | 本轮可证实的发现、限制和优先核查项 |
| [推理门槛对齐](THRESHOLD_ALIGNMENT.md) | 数据审计如何落到直接检测与分类，以及同版本对照结果 |
| [门槛运行对照](results/threshold_comparison.json) | 保存的调整前对照与现行输出计数、类别锚点和公开样例得分 |
| [字段与变换字典](FIELD_DICTIONARY.md) | 七源输入字段、实体身份、时间和指标的解析口径 |
| [真实标签契约](LABEL_CONTRACT.md) | 数据侧需提供的真实故障、正常窗口和映射信息 |
| [文件清单](results/file_inventory.csv) | 八城七源文件大小、行数、时间范围、逐文件解析质量 |
| [原始字段矩阵](results/raw_schema.csv) | 所有原始字段在各城市的存在情况 |
| [全量原始数据复核](results/raw_rescan_summary.json) | 56 个文件、521,491,569 行的独立流式扫描及构建审计对账 |
| [逐字段缺失统计](results/raw_field_quality_by_source.csv) | 七源 207 个字段的原始缺失量；逐城表见 `raw_field_quality.csv` |
| [校订字段字典](results/field_dictionary_reconciled.csv) | 对旧版 DYX 字典的 207 字段补全、量程纠偏与含义保留 |
| [磁盘量程异常单元](results/disk_io_above_100.csv) | `disk_io_util` 大于 100 的 47 个网元分钟单元，供导出方核准 |
| [条件缺失统计](results/routing_metric_role_quality.csv) | 路由按指标和设备角色分组；业务流按流类型分组见 `traffic_flow_field_quality_by_flow.csv` |
| [节点缺失行](results/node_partial_missing_rows.csv) | 22 条部分指标为 `NULL` 的节点记录及其具体字段 |
| [原始节点别名](results/raw_node_aliases.csv) | 完整扫描 1,450,970 条 node 原始行形成的 72 个角色到官方 ID 映射 |
| [实体来源覆盖率](results/entity_source_coverage.csv) | 80 个法定网元在七源中的分钟覆盖率 |
| [指标分布](results/metric_profile.csv) | 完整特征库的观测值和同实体相邻分钟绝对变化分位数 |
| [维度序列变化](results/dimension_series_delta.csv) | 11,006 条接口、路由、采集、业务维度序列的逐序列变化汇总 |
| [标识值清单](results/dimension_identifiers.csv) | 可从维度保留结构中恢复的接口、路由对端、采集目标、域名等标识 |
| [接口映射待核清单](results/interface_link_mapping_worklist.csv) | 482 个网元接口组合；对端网元和证据列留空待权威映射 |
| [服务域名映射待核清单](results/service_domain_mapping_worklist.csv) | 24 个目标域名；服务网元与证据列留空待权威映射 |
| [候选复核表](results/candidate_review.csv) | 当前直接检测的 403 个**候选**及其触发、根因、分类证据 |
| [候选城类分布](results/candidate_city_category.csv) | 候选按城市和故障类别的计数 |
| [运行摘要](results/summary.json) | 输入路径、特征 manifest 哈希、统计口径与计数 |
| [公开 Case 证据](PUBLIC_CASE_ANALYSIS.md) | 三个公开真值 Case 的原始数据归因与边界 |
| [旧版结果对账](RECONCILIATION.md) | 与 `AIOps_data_dyx/` 既有报告的差异和修订依据 |

`runs/` 保存本地预测、推理日志和证据审计，已从 Git 提交中排除；按下方命令即可重建。`results/` 保存数据侧统计表和紧凑运行摘要；填写映射时应复制工作清单，避免重新生成时覆盖人工结果。

同一直接检测代码在公开三例上的官方评测摘要为 TP 3、FP 0、FN 0、Total 96.011111，保存在 `results/threshold_comparison.json`。三例用于回归验收，不代表八城精度；详细运行文件可按下方流程在本地生成。

## 重跑

从仓库根目录执行；不需要重新解析约 96 GiB（103.2 GB）原始 CSV：

```bash
.venv/bin/python -m aiops_v2.run predict \
  --store data/feature_store/v2/stage1_all_cities_v2_20260926 \
  --detector direct \
  --output data_side/runs/stage1_direct_calibrated_20260927/predictions.jsonl \
  --inference-log data_side/runs/stage1_direct_calibrated_20260927/inference.json

.venv/bin/python -m aiops_v2.run audit \
  --store data/feature_store/v2/stage1_all_cities_v2_20260926 \
  --predictions data_side/runs/stage1_direct_calibrated_20260927/predictions.jsonl \
  --inference-log data_side/runs/stage1_direct_calibrated_20260927/inference.json \
  --output data_side/runs/stage1_direct_calibrated_20260927/evidence_audit.json

gzip -f data_side/runs/stage1_direct_calibrated_20260927/inference.json

PYTHONPATH=. .venv/bin/python data_side/build_reports.py \
  --predictions data_side/runs/stage1_direct_calibrated_20260927/predictions.jsonl \
  --inference data_side/runs/stage1_direct_calibrated_20260927/inference.json.gz \
  --audit data_side/runs/stage1_direct_calibrated_20260927/evidence_audit.json
```

`build_reports.py` 读取全部原始表头并扫描全部 node 行；其余逐文件解析质量取自特征库 manifest。另有独立的 `scan_raw_quality.py` 已逐行扫描约 96 GiB 的全部 56 个 CSV，核对行数和字段缺失，其结果按文件大小与修改时间缓存。已有预测与证据审计就绪时，一条命令重建并验证全部数据侧报告：

```bash
.venv/bin/python data_side/run_pipeline.py
```

需要强制重新读取所有原始文件时加 `--force-raw-scan`；正常运行会核查每个缓存文件的大小和修改时间。`metric_profile.csv` 的 edge 口径仅包含与直接检测有关的 Traffic 请求量、成功/错误、延迟、丢包和吞吐指标；其他原始字段仍完整列在字段矩阵中。原始缺失统计与特征张量 mask 分属不同层次，不能混为一谈。

## 尚不能由现有文件完成

第一批完整真实故障标签、确认正常的时间段、接口实际链路对端、域名到服务网元的权威映射均未在当前项目中提供。需要按 [真实标签契约](LABEL_CONTRACT.md) 补齐后，才能计算八城的 TP/FP/FN、根因 Top-5 和分类分数，并用正常窗口估计可用于标定的分布。已完成现有数据能够支持的四项原定数据工作；第一批逐 Case 真值归因须待标签交付后才能执行。

# 与既有 DYX 数据工作的对账

项目原定数据侧任务见 [`docs/requirements/AIOPS_OnePage.md`](../docs/requirements/AIOPS_OnePage.md)：基本情况和 pipeline、字段含义、多源含义、Case 归因。既有 [`AIOps_data_dyx/AIOps_DYX_Report.md`](../AIOps_data_dyx/AIOps_DYX_Report.md) 已完成一版框架。本目录在当前第一批数据和 v2 特征库上复核、补齐并标明不能从现有数据判定的部分。

| 项目 | 既有 DYX 结果 | 本轮核对与修订 |
| --- | --- | --- |
| 原始字段 | 实际扫描六源 188 字段；NetFlow 19 字段来自 schema reference | 八城七源 56 文件均已实际逐行扫描；207 个源内字段均有实际缺失统计，包含 NetFlow |
| 原始行数 | 六源 inventory 与构建审计 | 七源总计 521,491,569 行；逐文件与构建审计一致，CSV 列数错误记录为 0 |
| 缺失口径 | 主要给出全表/字段缺失 | 增加路由 `metric_name × node_type`、业务流 `flow_type` 条件缺失；区分结构性空列与观测缺失 |
| 字段量程 | `cpu_usage`、`disk_io_util` 记作 `ratio` | 观测最大值为 100 与 297.493；两列不可按 0–1 解释；物理单位与磁盘超 100 的原因待数据提供方核准 |
| Case 归因 | 既有报告分析公开样例 | 用独立脚本从三个公开 Case 的原始 node CSV 与官方真值重算前/中窗口证据及同城排名；见 [公开 Case 分析](PUBLIC_CASE_ANALYSIS.md) |
| 第一批标签 | 未见完整真值 | 当前项目仅有公开样例 `sample/ground_truth.jsonl`；第一批八城无法据此确定现行 403 个候选的真假或根因正确率 |

`results/field_dictionary_reconciled.csv` 保留了旧版项目释义，增加本轮实际缺失比例、活动流条件缺失比例与修订提示。`interpretation_status=project_interpretation_not_official` 表示含义和单位尚未经过主办方正式确认。原始缺失的绝对计数以 `results/raw_field_quality_by_source.csv` 为准；特征张量观测值量程以 `results/unit_scale_audit.csv` 为准。两者测量的层次不同。

本轮的流式 CSV 扫描验证了记录列数、字段缺失与行数；已有特征库 manifest 的解析审计另外给出时间、实体、数值及顺序检查。两项结合支持当前数据质量结论，但不能替代主办方对采集配置、拓扑、故障注入和字段物理单位的说明。

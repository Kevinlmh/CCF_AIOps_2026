# 多源混合诊断模型 v1.1 实施计划

> 本计划直接在 `hybrid-v1` 分支执行。每项生产代码先由失败测试定义，再实现最小正确行为。

## 任务 1：正式输入契约与审计

修改 `tests/test_multisource.py`、`preprocessing/observations.py`、`preprocessing/multisource.py`，加入双目录发现、Schema、正式清单和守恒统计测试与实现。

## 任务 2：指标语义与流式转换

新增 `config/metric_semantics.json`、`tests/test_metric_semantics.py`、`preprocessing/metric_semantics.py`，实现空值、方向、Counter 差分、State 和采样间隔逻辑。

## 任务 3：在线鲁棒检测

新增 `tests/test_streaming_detector.py`、`anomaly_detector/streaming_detector.py`，实现每序列 60 点状态、短长序列 warm-up、异常证据限流和检测诊断。

## 任务 4：七源流式管线与 NetFlow

新增 `tests/test_streaming_pipeline.py`、`preprocessing/streaming.py`，复用现有解析规则并将七源逐文件送入在线检测；NetFlow 通过临时 SQLite 聚合后流出，支持可配置 scratch 目录。

## 任务 5：时空事件拆分

新增 `tests/test_event_clustering.py`、`anomaly_detector/event_clustering.py`，拆分互不关联的并发城市故障，保留通过 traffic 关系连接的跨城传播事件。

## 任务 6：RCA 与分类增强

复核 `tests/test_graph_fusion.py` 与 `localization/graph_fusion.py` 中已有的直接证据、拓扑解释和症状惩罚；扩展 `tests/test_prototype_model.py`、`classification/prototype_model.py` 和 `config/model_v1.json`（其中 `version` 为 `1.1`），预留可配置的分类反证信号。数据质量先进入审计，待缺失语义确认后再进入评分。

## 任务 7：LLM/服务器接口

扩展 `tests/test_pipeline.py`、`tests/test_api_backend.py`、`run.py`、`models/api_backend.py`，加入候选压缩、输入模式、scratch、API 环境配置和服务器部署说明。

## 任务 8：集成、基准和协作文档

更新 `README.md`，新增 `docs/data/model-data-coordination.md` 和正式运行脚本；运行全测试、compileall、三个样例评测和正式小规模 smoke test，记录全量耗时估计与剩余风险。

## 执行结果（2026-09-14）

- 任务 1～8 的工程实现均已落入 `hybrid-v1` 工作区；
- `python3 -m unittest discover -s tests -v`：70 项通过；
- 三公开样例流式回归：`Total=98.855556`，`TP=3`、`FP=0`、`FN=0`；
- 正式数据预检：8 城市 × 7 来源共 56 个文件，103,189,563,750 字节，清单与表头合法；
- 正式 node 全文件流式基准：181,417 行、2,177,004 个观测、108 条序列，约 23 秒，峰值内存约 25.5 MiB；
- 尚未执行完整 96 GiB 正式推理；NetFlow 精确 SQLite 聚合是预计的主要耗时与临时磁盘瓶颈，首次全量运行必须记录实际时长、峰值 scratch 和事件数量。

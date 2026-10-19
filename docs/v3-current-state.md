# 当前状态与运行说明

更新时间：2026-10-19。当前 `v3` 分支为阶段归档，最终预测为 D3，634 条，记录得分 34.5224。详见 [结果记录](v3-optimization-results-2026-10-18.md) 与 [冻结清单](../artifacts/final-v3/manifest.json)。

## 已保留内容

- `aiops_v3/`、`data_side/`、`tests/v3/`：源码、配置及测试。
- `sample/`：官方公开三例及真值；`artifacts/public-sample-v1/`：历史样例预测与评估摘要。
- `artifacts/final-v3/`：D3 预测及冻结元信息；`artifacts/run-summaries/`：少量历史 manifest 与验证摘要。摘要中的输入路径和哈希仅记录当时状态，不能据此认为文件仍存在。
- `data_side/results/`：原始字段清单、字段解释及公开样例证据参考；`data_side/templates/`：人工复核空模板。其余逐行统计与审计表已删除。
- `docs/research/`、v4 架构回顾与本地研究计划导出。v4 架构说明描述历史分支，并非当前 v3 的实现。

## 已清理内容

本地 `data/`、旧 `outputs/v3/`、`outputs/v4/`；服务器项目 `data/`、`models/`、`outputs/` 及 `/data/liminghang/CCF_AIOps_2026/` 下的原始数据、解压包、SQLite 库和构建工作区均已删除。项目 GPU 服务已停止。Python 环境、服务器源码备份和用户共享依赖缓存保留。

## 公开样例运行

在仓库根目录完成 README 的安装后，可直接使用保留的样例。建库与输出目标必须是新路径：

```bash
python -m aiops_v3.run \
  --raw-root sample/case_001 \
  --source-profile stage1 \
  --build-store-to data/public-case001 \
  --output-dir outputs/public-case001 \
  --detector-profile conservative
```

该示例用于验证数据接入和规则推理，不是 D3 全量复现命令。

## 重新接入比赛数据

先重新取得原始 CSV，保留城市及时间段目录结构。第一批使用 `stage1` 来源配置；缺少业务探针与 FRR 日志的第二批、第三批使用 `stage2` 配置。

```bash
python -m aiops_v3.build_features \
  --raw-root /path/to/raw_batch --profile stage2 --preflight-only

python -m aiops_v3.build_features \
  --raw-root /path/to/raw_batch --profile stage2 \
  --store data/feature_store/new_batch

python -m aiops_v3.run \
  --input-store data/feature_store/new_batch \
  --output-dir outputs/new_batch \
  --detector-profile conservative \
  --routing-dimension-detection --routing-context-candidates --temporal-context
```

第三批前缀起点校验可显式开启 `--prefix-aware-blackhole`。NetFlow 聚合、四角色、事件发现和合并的参数分别见以下入口；不同版本的证据、prompt、模型响应与输入哈希不能混用。

```bash
python -m aiops_v3.netflow_stats --help
python -m aiops_v3.role_runner --help
python -m aiops_v3.discovery_runner --help
python -m aiops_v3.merge --help
python -m aiops_v3.prediction_audit --help
```

LLM 运行需另行安装服务器推理依赖并重新下载权重。`server_llm` 是早期单次诊断接口；四角色使用 `role_runner`。四角色在已有事件上逐字段应用结果，发现层另行提议事件；引用存在、角色接受率或预测数都不能替代真实诊断效果评估。

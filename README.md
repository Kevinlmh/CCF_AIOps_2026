# CCF AIOps Challenge 2026

当前 `main` 分支的模型实现位于 `aiops_v2/`。模型将七类原始观测转换为分钟级节点、边和日志特征，再完成时序异常检测、事件解码、根因排序和故障分类。

```text
七类 CSV → 分钟级特征存储 → 自监督时序模型 → 全局事件解码
        → 根因 Top-5 与故障分类 → predictions.jsonl
```

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[v2-model]'
```

## 构建、训练与预测

使用仓库中的公开样例运行完整流程：

```bash
python3 -m aiops_v2.run all \
  --data-root sample \
  --store data/feature_store/v2/sample \
  --checkpoint outputs/v2_0/sample/model.pt \
  --output outputs/v2_0/sample/predictions.jsonl \
  --inference-log outputs/v2_0/sample/inference.json
```

`--store` 应指向新的空目录；重复运行时请更换目录名。预测结果写入 `predictions.jsonl`，过程审计信息写入 `inference.json`。

模型流程、输入输出契约和各模块说明见 [`docs/design/model-v2-architecture.md`](docs/design/model-v2-architecture.md)。

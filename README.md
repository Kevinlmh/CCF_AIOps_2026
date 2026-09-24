# CCF AIOps Challenge 2026

当前 `main` 分支的模型实现位于 `aiops_v2/`。七类原始观测先转换为带缺失掩码的分钟级节点、边和日志张量，再按直接证据与症状证据分层检测、解码事件、排序根因和分类。

```text
七类 CSV → 分钟级张量/掩码 → 本机直接证据 + 业务症状 → 全局事件解码
        → 根因 Top-5 与故障分类 → predictions.jsonl
```

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[v2-model]'
```

## 构建与预测

使用仓库中的公开样例运行完整流程：

```bash
python3 -m aiops_v2.run all \
  --data-root sample \
  --store data/feature_store/v2/sample \
  --output outputs/v2_0/sample/predictions.jsonl \
  --inference-log outputs/v2_0/sample/inference.json
```

`--store` 应指向新的空目录；重复运行时请更换目录名。预测结果写入 `predictions.jsonl`，过程审计信息写入 `inference.json`。

模型流程、输入输出契约和各模块说明见 [`docs/design/model-v2-true-rebuild.md`](docs/design/model-v2-true-rebuild.md)。旧自监督时序模型仍可用 `--detector neural --checkpoint <路径>` 独立对照，服务器 LLM 复核接口默认关闭。

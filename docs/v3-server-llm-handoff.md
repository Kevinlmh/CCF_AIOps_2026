# v3 服务器 LLM 交接

当前 linux2 分阶段诊断实验与同步步骤见 [v3-llm-staged-experiment.md](v3-llm-staged-experiment.md)。以下保留旧 single 工作流交接记录，新实验请使用新文档中的环境和双卡服务配置。

LLM 只处理 Mac 端已经检出的事件。它可以在证据包的合法候选中重排根因 Top-5、修改故障类别，不能补事件、改时间或选候选集外的网元。所有响应由本地导入器再次校验，失败时逐事件回退规则结果。

## 冻结输入

`outputs/v3/server_bundle_20261002/` 已包含本轮源码、两批**原保守版**的 `evidence.jsonl`、`predictions.jsonl`、运行清单和 `SHA256SUMS`。它是独立快照，约 1.5 MB，不包含原始 CSV、模型权重或 `submit.py` 凭据。第一、第二批事件 ID 均从 1 开始，响应文件必须分开。

2026-10-03 的新推理结果位于 `outputs/v3/stage{1,2}_finalaudit_20261003/`。新证据包显式列出业务探针的观测者和服务组目标，服务器提示词也明确观测者不是已证实根因。上述旧 bundle 仍是旧版自洽快照；若使用新证据，请把当前 `aiops_v3/` 源码、`pyproject.toml` 与新 `evidence.jsonl` 一起交给服务器，重新生成响应。旧响应和新证据或新提示词的哈希不能混用。

在 Mac 项目根目录同步；将 `USER@SERVER` 改为实际 SSH 地址：

```bash
rsync -av outputs/v3/server_bundle_20261002/ USER@SERVER:~/ccf_aiops_v3/
```

服务器上先校验快照，再安装环境：

```bash
cd ~/ccf_aiops_v3
sha256sum -c SHA256SUMS
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e . vllm
```

`evidence_sha256` 绑定事件时间、证据值和候选排序；运行清单还记录输入特征、维度侧车、源码和输出哈希。响应格式由 `aiops_v3.server_llm` 自动生成，包含 `event_id`、`evidence_sha256`、合法 `root_cause_top5`、`fault_category`、`evidence_ids`、模型 repository/revision 和 prompt/schema/解码配置哈希。续跑时任一哈希或模型 revision 不匹配都会报错。

## 单卡小批量后全量

模型可先试 [Qwen3-8B-AWQ](https://huggingface.co/Qwen/Qwen3-8B-AWQ)。`<REVISION>` 填实际使用的模型 commit SHA。一个服务器终端启动 [vLLM JSON Schema 接口](https://docs.vllm.ai/en/stable/examples/features/structured_outputs/)：

```bash
CUDA_VISIBLE_DEVICES=0 vllm serve Qwen/Qwen3-8B-AWQ \
  --revision <REVISION> --host 127.0.0.1 --port 8000 --max-model-len 8192
```

另一个终端先跑 20 条，再用相同响应路径续跑全量：

```bash
cd ~/ccf_aiops_v3
source .venv/bin/activate
python -m aiops_v3.server_llm \
  --evidence evidence/stage1/evidence.jsonl \
  --output outputs/v3/server_stage1_responses.jsonl \
  --audit outputs/v3/server_stage1_audit.jsonl \
  --model Qwen/Qwen3-8B-AWQ --revision <REVISION> --limit 20
python -m aiops_v3.server_llm \
  --evidence evidence/stage1/evidence.jsonl \
  --output outputs/v3/server_stage1_responses.jsonl \
  --audit outputs/v3/server_stage1_audit.jsonl \
  --model Qwen/Qwen3-8B-AWQ --revision <REVISION>
python -m aiops_v3.server_llm \
  --evidence evidence/stage2/evidence.jsonl \
  --output outputs/v3/server_stage2_responses.jsonl \
  --audit outputs/v3/server_stage2_audit.jsonl \
  --model Qwen/Qwen3-8B-AWQ --revision <REVISION>
```

记录响应合法率、回退率、Top-5/类别变化、耗时和显存；固定同一证据包与规则版对照。尚未在服务器实际运行该流程。

## 回导

将两份响应同步回 Mac 后，分别用**原保守配置**运行 `python -m aiops_v3.run --diagnoser llm-jsonl --detector-profile conservative --llm-responses <对应批次响应文件>`，并指定对应 `--input-store` 和新的 `--output-dir`。输出目录必须为空。再用 `python -m aiops_v3.merge` 合并两批预测；检查 `fallback_count`、官方格式及与规则版的变化。不要覆盖根目录 `result.jsonl` 作为首次实验。

若要测试当前根目录 `result.jsonl` 对应的路由上下文版，应改用 `outputs/v3/stage{1,2}_routing_final_20261002/evidence.jsonl`，并在回导时加上 `--routing-dimension-detection --routing-context-candidates --temporal-context`。保守版的响应哈希不能复用于这组证据。

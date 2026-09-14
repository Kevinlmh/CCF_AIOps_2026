# 四卡服务器推理接口

## 推荐部署形态

CSV 预处理由 CPU、内存和本地 NVMe 完成；GPU 服务只负责事件级 LLM 复核。推荐使用事件缓存把两个阶段拆开，避免 LLM 故障后重新扫描 96 GiB 数据。

## 阶段一：CPU 流式检测并保存事件

```bash
python baseline/bian/run.py \
  --data-root data/stage1/regions \
  --ingestion-mode streaming \
  --scratch-dir /fast-nvme/aiops-scratch \
  --event-cache outputs/stage1_events_v1_1.json \
  --decision-backend local \
  --output outputs/stage1_local_predictions.jsonl \
  --inference-log outputs/stage1_local_inference.json
```

`--scratch-dir` 必须位于空间充足的本地 NVMe。严格模式默认要求八城市七来源全部存在；仅调试单城市时显式添加 `--allow-partial-input`。

## 阶段二：OpenAI-compatible/vLLM 服务

服务器可使用任何兼容 `/v1/chat/completions` 的服务。vLLM 示例：

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3 python -m vllm.entrypoints.openai.api_server \
  --model /models/your-model \
  --served-model-name aiops-model \
  --tensor-parallel-size 4 \
  --host 0.0.0.0 \
  --port 8000 \
  --api-key "$AIOPS_LLM_API_KEY"
```

7B 模型通常更适合单卡多副本；32B/70B 可使用四卡张量并行。具体方式按模型大小和实测吞吐选择。

客户端复用事件缓存：

```bash
export AIOPS_LLM_API_BASE='http://127.0.0.1:8000/v1'
export AIOPS_LLM_API_KEY='replace-with-runtime-secret'

python baseline/bian/run.py \
  --data-root data/stage1/regions \
  --event-cache outputs/stage1_events_v1_1.json \
  --reuse-event-cache \
  --decision-backend api \
  --model aiops-model \
  --llm-workers 4 \
  --api-timeout 180 \
  --output outputs/stage1_llm_predictions.jsonl \
  --inference-log outputs/stage1_llm_inference.json
```

API 地址和密钥只从参数/环境变量读取，不写入源码、事件缓存和推理日志。`--llm-workers` 仅对 API 后端并发；Transformers 进程内后端保持串行，避免线程争用同一模型。

## 接口契约

请求采用 Chat Completions：

```json
{
  "model": "aiops-model",
  "messages": [{"role": "user", "content": "<prompt + INPUT_JSON>"}],
  "temperature": 0,
  "max_tokens": 512,
  "response_format": {"type": "json_object"}
}
```

响应必须含 `choices[0].message.content`，其中 content 是符合对应 Stage Schema 的 JSON 文本。客户端会执行严格 Schema 校验、有限重试和密钥脱敏。

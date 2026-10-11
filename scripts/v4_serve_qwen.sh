#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_DIR"
source "$HOME/.venvs/aiops-vllm-cu129/bin/activate"
test -f "$REPO_DIR/models/Qwen3-8B-AWQ/config.json"
# Older AWQ tokenizer templates omit the enable_thinking switch. Keep model
# files untouched and generate a service-local template with a closed think tag.
mkdir -p "$REPO_DIR/outputs/v4/model_service"
python - "$REPO_DIR" <<'PY'
import json, sys
from pathlib import Path
repo=Path(sys.argv[1])
template=json.loads((repo/'models/Qwen3-8B-AWQ/tokenizer_config.json').read_text())['chat_template']
needle="{{- '<|im_start|>assistant\\n' }}"
if template.count(needle) != 1:
    raise ValueError('unexpected tokenizer template; inspect before replacement')
template=template.replace(needle,"{{- '<|im_start|>assistant\\n<think>\\n\\n</think>\\n\\n' }}")
(repo/'outputs/v4/model_service/qwen3_nonthinking.jinja').write_text(template,encoding='utf-8')
PY
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_NO_USAGE_STATS=1
export CUDA_VISIBLE_DEVICES="${AIOPS_MODEL_GPUS:-0,1}"
exec vllm serve "$REPO_DIR/models/Qwen3-8B-AWQ" \
  --served-model-name Qwen/Qwen3-8B-AWQ \
  --tensor-parallel-size 2 --max-model-len 32768 --max-num-seqs 1 \
  --gpu-memory-utilization 0.88 --enforce-eager \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --chat-template "$REPO_DIR/outputs/v4/model_service/qwen3_nonthinking.jinja" \
  --default-chat-template-kwargs '{"enable_thinking":false}' \
  --host 127.0.0.1 --port 8000

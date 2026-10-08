# v3 分阶段 LLM 诊断实验

本次升级保留原检测器：事件数量和时间边界由规则决定。LLM 分别分析设备、排序根因、判断类别，Python 校验器分别决定根因和类别是否采纳。预测字段仍符合官方协议。

默认命令继续使用原规则行为；`server_llm --workflow single` 保留旧单次请求。新实验需要 `run --diagnostic-context`、`server_llm --workflow staged` 和回放时的 `--llm-policy evidence-gated`。

## 改动及限制

- 证据卡包含候选设备的故障前 10 分钟、事件期间、后 5 分钟的观测数、期望数、中位数及范围；保留正常指标、采集缺口、逐 peer/interface 的变化、异常起点和可观察恢复点。少于 3 个参考观测时不产生新的异常支持。窗口用于诊断，不修改事件边界。
- 直接信号及每设备最多 8 个聚合指标、6 个维度序列、事件最多 20 条去重日志参与证据包。优先 CPU/process/memory/disk/scrape 和已触发的指标。维度异常按局部规则识别，采用规则检测器的指标阈值；这仍是有限证据检索，不保证找全所有故障。
- 设备分析每次处理 3 个候选，设备按名字排列，不提供规则根因、候选分数或规则类别。根因排序最多使用 64 条紧凑证据；请求材料限制在约 12 KB，优先保留已引用证据和直接信号。独立分类读取经过根因校验后实际采用的 Top1，并优先读取该根因自身证据，不读取规则答案。字节限制不是精确 token 计数，实际上下文限制仍由模型服务校验。候选池仍沿用 v3，未扩展为全网搜索，也未增加未验证的拓扑路径。
- 输出使用 `E001` 等短引用 ID，写响应时还原为真实 ID。数组最多 4/6 条引用，不使用 vLLM 不支持的 `uniqueItems`（见 [vLLM 官方 grammar 校验实现](https://docs.vllm.ai/en/latest/api/vllm/v1/structured_output/backend_xgrammar/)）；Python 检查重复引用、五个不同候选、合法类别组合和哈希。单次输出最多 1024 tokens，关闭 Qwen thinking；无效/截断响应至多重试一次并附校验错误，HTTP 400 直接记审计。
- 改 Top1 或引入新 Top5 成员，必须引用该设备自身直接异常或局部指标异常。探针观察到业务异常不等于探针故障；服务组异常无法映射到具体 VM 时，不允许据此改实例根因。类别改动必须引用最终 Top1 自身的对应异常，或该 Top1 所属目标城市的服务组症状。日志文本单独不能通过这些门槛。
- 根因或类别不通过时，仅回退相应字段。`accepted` 表示完整响应通过格式/引用校验，**不表示所有建议都被采纳，更不表示诊断正确**；看回放 `audit.jsonl` 的 `root_review`、`category_review` 和 `fallback_reason`。未获得完整响应的事件整体回退规则。
- 新证据改变 `evidence_sha256`；新工作流、提示、代码和解码设置改变 `prompt_sha256`。旧 single 响应不能用于新 staged 文件续跑。第一、第二批必须分别处理，因为事件 ID 在批次内编号。
- 无隐藏真值时不能保证提高分数。当前策略刻意限制无直接证据的实例替换，可能保留原有漏报和错误；后续再单独研究检测阶段。

## 1. Mac 推送 GitHub，服务器从 v3 分支更新

代码交付路径是 **Mac 本地提交 → GitHub `v3` 分支 → linux2 更新仓库**。Git 传输源码和说明；服务器现有的特征库、模型、虚拟环境和实验输出保留在原位置，不需要通过 Mac 再次复制。

### 1.1 Mac：提交并推送

本次修改由 Codex 在 Mac 提交并推送到 `origin/v3`；推送成功后按第 1.2 节更新服务器。以后继续改代码时，在 Mac 项目终端使用下面的流程，`git add` 只列出本次要发布的文件：

```bash
cd /Users/likevin/lmh/CCF_AIOps_2026
git branch --show-current
git status --short
git add aiops_v3 tests/v3 docs/v3-llm-staged-experiment.md \
  docs/v3-server-llm-handoff.md docs/plans/v3-llm-diagnosis-upgrade.md
git commit -m "feat: add staged evidence-gated v3 LLM diagnosis"
git push origin v3
git rev-parse HEAD
```

当前分支应为 `v3`；已经提交且没有新改动时，不需要再次 `git commit`。保留最后输出的 commit SHA，用于核对服务器是否拿到同一版本。

### 1.2 linux2：更新已有仓库（当前服务器使用这一节）

`git clone` 用于首次下载仓库。当前服务器已有 `/home/liminghang/work/CCF_AIOps_2026/.git`，因此在**服务器终端**使用 `fetch` 和 `pull` 更新；不能直接向已有的非空项目目录再次 clone。

```bash
cd /home/liminghang/work/CCF_AIOps_2026
git remote get-url origin
git status --short
git fetch origin v3
git switch v3
git pull --ff-only origin v3
git rev-parse HEAD
git rev-parse origin/v3
```

`origin` 应为 `https://github.com/Kevinlmh/CCF_AIOps_2026.git`。服务器 `HEAD` 应与本次 Mac 推送的 SHA 一致；`HEAD` 与 `origin/v3` 也应一致。`--ff-only` 避免更新时自动创建合并提交。

**如果 `switch` 或 `pull` 提示本地修改会被覆盖**：此前在服务器手动修过 `server_llm.py` 和 `optional_llm.py`，本次 GitHub 源码已经包含相应 schema 修复。先查看这两份文件的差异，确认只包含此前的手工修复；随后用仓库版本替换这两份文件，再更新：

```bash
git diff HEAD -- aiops_v3/server_llm.py aiops_v3/optional_llm.py
git restore --source=HEAD --staged --worktree -- \
  aiops_v3/server_llm.py aiops_v3/optional_llm.py
git switch v3
git pull --ff-only origin v3
git rev-parse HEAD
```

这条 `restore` 会丢弃上述两份文件的本地修改。其他文件有未提交改动、或者 `--ff-only` 报分支分叉时，先查看具体状态再处理；不要把这个问题当成需要删除整个项目目录。此次源码更新不需要重装依赖，原 `.venv` 和已启动的 vLLM 服务可以继续使用；新请求由更新后的 Python 客户端构造。

### 1.3 首次下载到新目录时才使用 clone

如果换到尚未放置仓库的服务器，在目标项目目录不存在时运行：

```bash
mkdir -p /home/liminghang/work
cd /home/liminghang/work
git clone --branch v3 --single-branch \
  https://github.com/Kevinlmh/CCF_AIOps_2026.git CCF_AIOps_2026
cd CCF_AIOps_2026
git rev-parse HEAD
```

新 clone 不包含特征库、模型和 `.venv`；这些内容在 `.gitignore` 中，需要另行准备。当前 linux2 已有这些内容，按第 1.2 节更新后直接继续下面的实验步骤。

## 2. 服务器：按相同检测参数重新生成丰富证据

在服务器一个新终端运行；保留模型服务所在终端。

```bash
cd /home/liminghang/work/CCF_AIOps_2026
source .venv/bin/activate
export CUDA_VISIBLE_DEVICES=""
STORE1="data/feature_store/v3/stage1_canonical_20260930"
STORE2="data/feature_store/v3/stage2_canonical_20261001"
BASE_ROOT="outputs/v3/3080_rules_enriched"
LLM_ROOT="outputs/v3/3080_qwen3_staged"
REPLAY_ROOT="outputs/v3/3080_qwen3_staged_replay"
MODEL="Qwen/Qwen3-8B-AWQ"
REV="cb7d6a337aadb4d2082ed0dcef1032e4f8645194"
```

首次使用下面的新目录；重做实验时给三者统一添加 `_2` 等后缀，不要混入旧响应。规则输出和回放输出目录需要为空；LLM 响应目录可在相同配置下续跑。

```bash
python -m aiops_v3.run \
  --input-store "$STORE1" --output-dir "$BASE_ROOT/stage1" \
  --detector-profile conservative --routing-dimension-detection \
  --routing-context-candidates --temporal-context --exact-bgp-session-merge \
  --diagnostic-context
python -m aiops_v3.run \
  --input-store "$STORE2" --output-dir "$BASE_ROOT/stage2" \
  --detector-profile conservative --routing-dimension-detection \
  --routing-context-candidates --temporal-context --diagnostic-context
```

检测参数与此前一致，因此预期仍为 319/76；以实际输出为准。新证据不修改 `predictions.jsonl`。如果输入或原本服务器代码存在其他差异，先比较事件再接 LLM。

## 3. 服务器：20 条分阶段诊断，检查审计后续跑

```bash
curl -fsS http://127.0.0.1:8000/v1/models
python -m aiops_v3.server_llm \
  --evidence "$BASE_ROOT/stage1/evidence.jsonl" \
  --output "$LLM_ROOT/stage1_responses.jsonl" --audit "$LLM_ROOT/stage1_audit.jsonl" \
  --model "$MODEL" --revision "$REV" --workflow staged --limit 20
```

审计保留每一步的状态、finish_reason、token 用量、耗时、响应节选及错误正文。先检查是否出现上下文超限、语法不支持、截断或设备覆盖错误。每事件通常约 5–6 次模型请求，耗时将高于旧单次请求；暂未测量真实服务器耗时及提分效果。

```bash
python - <<'PY'
import json
from collections import Counter
from pathlib import Path
p = Path("outputs/v3/3080_qwen3_staged/stage1_audit.jsonl")
rows = [json.loads(s) for s in p.read_text().splitlines() if s.strip()]
latest = {r["event_id"]: r for r in rows}
print("最近状态:", dict(Counter(r["status"] for r in latest.values())))
print("根因校验:", dict(Counter(r.get("root_review") for r in latest.values())))
print("类别校验:", dict(Counter(r.get("category_review") for r in latest.values())))
for r in latest.values():
    if r["status"] != "accepted":
        print(json.dumps(r, ensure_ascii=False))
PY
```

如果更换了目录后缀，同步修改上述审计路径。确认小批量响应后使用相同路径续跑：

```bash
python -m aiops_v3.server_llm \
  --evidence "$BASE_ROOT/stage1/evidence.jsonl" \
  --output "$LLM_ROOT/stage1_responses.jsonl" --audit "$LLM_ROOT/stage1_audit.jsonl" \
  --model "$MODEL" --revision "$REV" --workflow staged
python -m aiops_v3.server_llm \
  --evidence "$BASE_ROOT/stage2/evidence.jsonl" \
  --output "$LLM_ROOT/stage2_responses.jsonl" --audit "$LLM_ROOT/stage2_audit.jsonl" \
  --model "$MODEL" --revision "$REV" --workflow staged
```

## 4. 服务器：回放、合并和一致性审计

```bash
python -m aiops_v3.run \
  --input-store "$STORE1" --output-dir "$REPLAY_ROOT/stage1" \
  --detector-profile conservative --routing-dimension-detection \
  --routing-context-candidates --temporal-context --exact-bgp-session-merge \
  --diagnostic-context --diagnoser llm-jsonl \
  --llm-policy evidence-gated --llm-responses "$LLM_ROOT/stage1_responses.jsonl"
python -m aiops_v3.run \
  --input-store "$STORE2" --output-dir "$REPLAY_ROOT/stage2" \
  --detector-profile conservative --routing-dimension-detection \
  --routing-context-candidates --temporal-context --diagnostic-context \
  --diagnoser llm-jsonl --llm-policy evidence-gated \
  --llm-responses "$LLM_ROOT/stage2_responses.jsonl"
python -m aiops_v3.merge \
  --input "$REPLAY_ROOT/stage1/predictions.jsonl" \
  --input "$REPLAY_ROOT/stage2/predictions.jsonl" --output "$REPLAY_ROOT/merged.jsonl"
python -m aiops_v3.prediction_audit \
  --stage1-run "$REPLAY_ROOT/stage1" --stage2-run "$REPLAY_ROOT/stage2" \
  --merged "$REPLAY_ROOT/merged.jsonl" --report "$REPLAY_ROOT/audit.json"
```

## 5. 不再调用模型，生成字段对照

```bash
python -m aiops_v3.diagnosis_ablation \
  --baseline-root "$BASE_ROOT" --reviewed-root "$REPLAY_ROOT" \
  --output-dir outputs/v3/3080_qwen3_staged_ablation
```

输出：`rules.jsonl`、`both.jsonl`、`roots_only.jsonl`、`category_only.jsonl` 和 `comparison.json`。分别对应规则、采用回放全部改动、仅采用回放根因、仅采用回放类别。全部按批次内事件 ID 对齐，检查输入/检测设置、时间、官方格式与重复事件，记录输入输出哈希及变化数量。

字段拼接对照用于诊断分数变化的来源，**不是额外一次有因果一致性的诊断**：类别判断可能原本依赖 LLM 改后的根因。如果需要真正“仅改类别”的独立策略实验，另建回放目录，加 `--llm-application category-only`；校验器会重新针对规则 Top1 检查类别证据。`roots-only` 同理。无论哪种对照，都需单独提交才能获知隐藏评测分数。

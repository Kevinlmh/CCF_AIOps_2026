# v4 服务器实验：压缩存储、保守候选和三个公开样例

第一批按原四角色架构从完整 CSV 到 LLM 预测的配置、阶段续跑和磁盘预留，见 [端到端实验与第一批全量运行](v4-full-experiment-status.md)。这里的 `discovery_only` 和资源规则对照用于排查上游或比较诊断，不能代替主实验。

2026-10-11 第一批任务已在 tmux `aiops-v4-stage1-full` 启动。输出使用账号拥有的 `/data/liminghang/CCF_AIOps_2026/outputs/v4/experiments/stage1_llm_full`；日志在项目 `outputs/v4/experiments/stage1_llm_full.log`。运行期间保持代码和配置不变。

## 这次改动

此前每文件前 1,000 行的试跑只有 51,481 条输入，但生成了 41,095 个窗口、3,303 个候选事件和 2,455 个证据包，占约 1.8 GiB。主要问题是逐窗口重复 JSON 元数据、数据库再次保存相同信息，以及把统计偏离、稀有簇、日志告警等直接取并集作为候选。NetFlow 全量逐行暂存还会放大临时空间。

新的配置是 `configs/v4/conservative-compact.json`，包含以下选择：

- feature/state/evidence 的 JSONL 从第一行开始写入 gzip；SQLite 的 JSON 载荷压缩，索引列仍可查询。所有窗口和状态仍保留，完整性校验使用解压后的逻辑内容和封存文件哈希。最终预测和模型交互日志仍为普通 JSONL。
- NetFlow 边读边按网元、接口、协议和分钟汇总，内存缓存有上限，溢出的窗口存入 SQLite。保留数量和、均值/方差、极值、有效/缺失/非法记录计数和原始引用。此模式不计算逐流分位数、不同值数量或变化次数；状态数值特征仍使用数量和。
- 候选阈值由 6 调到 8；稀有簇不能单独入选。弱异常还需要达到最小变化幅度并连续出现至少两个窗口；窗口起点不会因为等待第二个窗口而后移。BGP 等直接协议中断保留单窗口准入。
- 统计偏离还需要故障机制信号。节点检查 CPU/磁盘忙碌率 ≥80%、可用内存 ≤15%、文件系统/ inode 使用率 ≥90% 等资源压力；接口检查丢包/错误 ≥1 包每秒或 ≥1% 包率；业务流检查错误计数、≥0.5 秒延迟或 ≥1% 丢包。普通字节率、磁盘吞吐、QPS 变化只作为上下文。上述阈值是可验证的准入假设，不能当故障标签，也可能漏掉较轻或很短的故障。
- 持续资源压力可独立进入候选，避免故障占据大部分参考区间时被中位数吸收。相对线索使用本序列首个有效节点窗口：可用内存减少至少 5 个百分点，或者 CPU 上升至少 10 个百分点且 load1 同时上升至少 0.5。首个观测不是已知健康，起点就已受影响或基准字段缺失时仍可能漏检。规则关闭的消融不增加这类线索。
- 若完整参考中协议指标始终为 0，没有观测到出现/恢复，则保留为静态缺席上下文，不直接当协议中断。参考不足时仍保留原始的零值线索，交给后续诊断。
- NetFlow 和 FRR 日志作为上下文保留，不再单独生成故障候选。该选择可能降低只在流量/日志中出现的故障召回率，需要通过实验判断。
- 被抑制的窗口保留触发信号和抑制原因，不会被描述成“无当前触发”的反证。它们仍可通过只读查询取回。
- 默认提示每类先提供 2 个观测及检索入口，避免重复引用和元数据挤满上下文。完整候选清单保留，缺少的指标/原文通过只读工具查询；未完成工具分块读取的证据仍不能引用。
- 模型返回非法 JSON 时最多再生成一次，仍受原有轮数/字符预算限制，两次响应和用量都记录。合法但不符合证据/角色约束的答案仍拒绝；程序不自动补齐根因或类别。
- 事件确认可用 `window_ids=["*"]` 表示整份 manifest。只允许单个 assessment 使用，程序展开实际 ID 并校验连续性、全量归属和已交付的支持证据；多个事件仍需要明确的窗口分配。该约定减少模型复制长哈希 ID 时的遗漏。

单独调用 feature/state 模块的默认选项保留旧行为；新实验要明确使用上述配置。状态候选数不等于最终故障数，最终还经过 LLM 确认、定位、分类和复核。

## 相同前缀输入的测量

在 linux2 上，仍扫描每文件前 1,000 行，共 51,481 行、41,095 个窗口：

| 指标 | 原先离线运行 | 本次保守压缩运行 |
|---|---:|---:|
| 候选事件 | 3,303 | 22 |
| 证据包 | 2,455 | 22 |
| 最终目录大小 | 约 1.8 GiB | 251.83 MiB |
| 进程峰值 RAM | 未记录 | 65.6 MiB |
| 墙钟耗时 | 未记录 | 3 分 47 秒 |

新运行位于 `outputs/v4/experiments/stage1_prefix_joint_pressure`，完整性校验通过。以上是解析/候选/证据阶段，模型调用和最终预测均为零；降低候选数并不能证明检测更准确。目录大小是结束时占用，峰值 RAM 由 `/usr/bin/time -v` 测量，未测量整个运行的磁盘峰值。

## 服务器准备

在服务器 `/home/liminghang/work/CCF_AIOps_2026` 下操作：

```bash
cd /home/liminghang/work/CCF_AIOps_2026
source .venv/bin/activate
```

模型使用已下载的 `models/Qwen3-8B-AWQ` 和已有的 `~/.venvs/aiops-vllm-cu129`。启动脚本只监听 localhost，使用 GPU 0、1，不下载模型。它生成独立的非思考模板，原模型文件不修改。当前 AWQ 的 tokenizer 模板没有 `enable_thinking` 分支，因此只设置请求参数无法关闭 `<think>` 输出。

没有模型服务时启动：

```bash
mkdir -p outputs/v4/model_service
tmux new-session -d -s aiops-v4-model \
  'cd /home/liminghang/work/CCF_AIOps_2026 && bash scripts/v4_serve_qwen.sh > outputs/v4/model_service/vllm.log 2>&1'
```

```bash
tail -n 20 outputs/v4/model_service/vllm.log
curl -fsS http://127.0.0.1:8000/v1/models
```

重复运行前检查已有服务，不要重复启动同名 tmux 会话。查看服务终端使用 `tmux attach -t aiops-v4-model`；退出查看按 `Ctrl+b`，然后 `d`。停止本次服务使用 `tmux send-keys -t aiops-v4-model C-c`。

工具调用和非思考模板依据 [Qwen 的 vLLM 部署说明](https://qwen.readthedocs.io/en/latest/deployment/vllm.html)；Qwen3 使用 Hermes 工具解析器。服务预留 32,768 token 上下文，逐条诊断，不开放外部访问。

## 运行全部三个公开样例并评分

以下运行扫描 `sample/case_001`、`case_002`、`case_003` 的完整 CSV，调用本地模型。全部预测完成后才读取 `sample/ground_truth.jsonl`，对合并结果和每个 case 分别调用仓库中的官方评测器。不会根据答案筛选预测、修改窗口或提前给模型答案。

```bash
export AIOPS_LOCAL_LLM_KEY="local-vllm"
python scripts/v4_public_samples.py \
  --output-dir outputs/v4/official_samples_fresh \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-8B-AWQ
```

密钥是本地无认证服务所需的客户端占位值，不是外部 API 密钥。若希望 SSH 断开后仍继续：

```bash
tmux new-session -d -s aiops-v4-public \
  'cd /home/liminghang/work/CCF_AIOps_2026 && export AIOPS_LOCAL_LLM_KEY=local-vllm && .venv/bin/python scripts/v4_public_samples.py --output-dir outputs/v4/official_samples_fresh > outputs/v4/official_samples_fresh.log 2>&1'
tail -f outputs/v4/official_samples_fresh.log
```

已有完整 case 会在核对配置、代码指纹和原始文件大小/修改时间后复用。如果代码或数据变了，请用新的目录，如 `official_samples_compact_2`。失败或未封存的 case 不会静默复用。

### 复用当前缓存，只重跑模型

服务器已完整扫描三个样例并封存候选和证据，位于 `outputs/v4/official_samples_compact`。修改模型提示或后端参数时，可固定这些候选，单独比较诊断效果，避免重读 CSV：

```bash
export AIOPS_LOCAL_LLM_KEY="local-vllm"
python scripts/v4_public_samples.py \
  --reuse-evidence-from outputs/v4/official_samples_compact \
  --output-dir outputs/v4/official_samples_stable_repeat
```

该模式校验原运行封存、原始文件清单、检测/解析配置、证据和诊断产物哈希；分别记录候选生产版本与当前诊断版本。它复用历史检测输出，不声称重新执行了最新检测代码。三个源 case 必须来自同一生产代码版本。修改检测实现或阈值后应重新生成源缓存。缓存源目录是后续诊断的输入，应保留完整文件。

本地诊断固定 `temperature=0`、输出上限 1,536 token、请求超时 300 秒，并顺序处理三个 case，避免单并发模型服务的排队超时。更换模型或服务参数时使用新诊断目录。

该入口启用 `structured_roles=true`：本地 vLLM 用必选函数参数提交结论和完整窗口分组，程序继续校验引用、持续性和角色证据，不自动补写模型判断。候选指标按时间紧凑提供；定位/分类只接收确认事件范围，不接收确认角色的解释。最终状态查询每页最多一条，补充查询最多两轮。增加资源摘要、依据先行和缩减初始展示的实验效果更差，已撤回默认行为，详见审查记录。

自定义 `aiops_v4.experiments` HTTP 配置需要显式在 `backend` 中设置 `"structured_roles": true` 才使用同一协议。旧配置和独立模块的兼容性默认值仍为 false。

查看结果：

```bash
cat outputs/v4/official_samples_stable/case_scores.json
cat outputs/v4/official_samples_stable/official_score.json
cat outputs/v4/official_samples_stable/public_summary.json
```

主要输出：

| 文件 | 内容 |
|---|---|
| `case_001/agents/predictions.jsonl` 等三个文件 | 每个样例的最终预测 |
| `predictions.jsonl` | 三个样例合并预测，保留全部额外预测 |
| `case_scores.json` | 每例 Total、AD、RCA、Major、Minor、TP/FP/FN |
| `official_score.json` | 三例合并评分及逐真值匹配 |
| `public_summary.json` | 扫描/候选/调用/预测计数、失败数和代码/输入指纹 |
| `case_*/agents/roles.jsonl` | 模型交互、只读查询和错误码 |

也可再次直接运行官方评分命令：

```bash
python -m aiops_challenge_2026.evaluator \
  --ground-truth sample/ground_truth.jsonl \
  --predictions outputs/v4/official_samples_stable/predictions.jsonl \
  --report outputs/v4/official_samples_stable/rescore.json
```

仅排查解析和候选、暂不调用模型时：

```bash
python scripts/v4_public_samples.py \
  --output-dir outputs/v4/official_samples_discovery \
  --discovery-only
```

该模式不生成评分，模型调用数和预测数均为零。

## 修复前失败记录

实际诊断与官方评分位于 `outputs/v4/official_samples_diagnosis`，合并评分已用官方 CLI 再次核对：

| 样例 | 完整输入行数 | 候选事件 / 证据包 | 模型请求 | 最终预测 | 官方 Total |
|---|---:|---:|---:|---:|---:|
| case_001 | 604,284 | 1 / 1 | 3 | 0 | 0 |
| case_002 | 506,482 | 1 / 1 | 2 | 0 | 0 |
| case_003 | 520,527 | 2 / 2 | 5 | 0 | 0 |

合并 Total、AD、RCA、Major、Minor 均为 0，TP=0、FP=0、FN=3。所有候选均已选择并实际请求本地 Qwen，`diagnosis_complete=false`；**LLM 端到端样例尚未通过**，不能以单元测试通过替代这一结果。

错误都发生在事件确认：3 个包的最终 JSON 不合法（包括多余的结束括号，重试后仍有），另 1 个包使用未知/重复的窗口 ID。完整性与证据约束将这些结果拒绝，定位、分类和复核没有进入执行。缩小工具分页后，这轮已无 HTTP 400 或请求超时，当前瓶颈是输出协议与完整窗口分配。

预测结束后进行的诊断显示：对应三个真实故障节点的候选区间 Dice 分别为 0.904、0.911、0.897，第三例另有 monitor 节点候选。该数值只用于分析候选阶段，不是最终 AD 分或完整预测评分，没有用答案补写预测。详情保存在 `failure_analysis.json`。

上述记录是修复前的结果。后续已增加结构约束、窗口分组反馈、紧凑真实指标、前后观测以及确定顺序的证据校验；最新对照和解释见 [审查与样例回归](v4-audit-and-sample-results.md)。原失败目录保留供定位问题，不能代表修改后的模型结果。

## 本次最终样例结果

`outputs/v4/official_samples_stable` 已跑完全部三个样例并用官方 CLI 复核，48 次模型请求、4 条预测，角色协议失败和暂缓均为零。

| 样例 | Total | TP / FP / FN |
|---|---:|---:|
| case_001 | 94.9333 | 1 / 0 / 0 |
| case_002 | 86.7333 | 1 / 0 / 0 |
| case_003 | 80.9117 | 1 / 1 / 0 |
| 三例合并 | **86.6436** | **3 / 1 / 0** |

合并 AD=33.3103、RCA=40、大类=10、小类=3.3333。第二例内存和第三例磁盘 I/O 被误判为 CPU 压力，第三例额外监控节点也被接受。下面的规则对照为 93.3103；当前样例没有证明四角色 LLM 比规则更好。失败的复杂提示实验已撤回，结果与原因保存在审查记录中。

## 不调用模型的资源规则对照

```bash
python scripts/v4_rules_control.py \
  --evidence-root outputs/v4/official_samples_compact \
  --output-dir outputs/v4/official_samples_rules_control_repeat
```

该对照使用所有已封存资源候选，仅输出节点资源压力类别、本机根因和观测区间。它不覆盖路由、链路和服务故障，不冒充 LLM 诊断，也不会按答案删除额外事件。已有 `official_samples_rules_control` 的合并 Total 为 93.3103，TP=3、FP=1、FN=0；答案在所有预测生成后读取。

## 清理和后续全量

已删除旧 `outputs/v4/experiments/stage1_prefix_offline` 的中间产物，原配置、计数、时间和指纹保存在 `outputs/v4/diagnostics/stage1_prefix_offline_summary.json`。保留原始 CSV、模型、其他结果和 `submit.py`。

压缩后的 feature/state/evidence 可以重复查询和诊断；其中 `evidence.sqlite` 支持按需读原始引用。当前代码不会删除已封存运行中的单个文件：缺少文件会导致校验失败。需要清理时，先保留评分和实验摘要，再删除明确不需要的整个运行目录。

公开样例只有三例，分数用于检查完整流程及具体错误，不代表比赛隐藏集水平。更紧的候选规则可能减少误报，也可能漏掉短时或单一流量异常。不能根据三个标签直接推断全量最优阈值。

全批次的压缩产物规模和峰值尚需实际测量，不应把前缀文件大小直接线性外推或承诺一定能放进剩余磁盘。先确认样例中的模型角色成功率，再决定全量实验；全量解析耗时仍主要受 CSV 扫描、窗口数和状态模型拟合影响。

后续仅运行第一批全量解析、候选和证据时，可用下面的入口。它仍不调用模型，不生成提交结果：

```bash
python -m aiops_v4.experiments run \
  --config configs/v4/conservative-compact.json \
  --output-dir outputs/v4/experiments/stage1_compact_all
```

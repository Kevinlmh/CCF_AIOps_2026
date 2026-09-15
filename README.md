# AIOps Challenge 2026

本仓库提供 AIOps Challenge 2026 的样例数据、评测工具和参考 Baseline。

GitHub 仓库：`https://github.com/Kevinlmh/CCF_AIOps_2026`

分支说明：

- `official-baseline`：官方 Gitee Baseline 的原始快照；
- `hybrid-v1`：当前多源混合模型 v1.4 开发分支；
- `main`：已发布的稳定分支，功能分支完成团队复核后再合并。

远端 `upstream` 指向官方 Gitee，`origin` 指向本项目 GitHub。

## 多源混合模型 v1.4

当前分支已在原始 BiAn Baseline 上加入可直接运行的多源混合模型：

1. 统一读取 node、interface、routing、scrape health、traffic flow、NetFlow、FRR syslog 七类数据；
2. 用滚动中位数与 MAD 完成无监督异常检测，并按赛事 1～30 分钟先验切分事件；
3. 融合异常强度、持续性、时间先后、来源多样性、直接性和公开拓扑完成 RCA Top5；
4. 用覆盖官方 28 类故障的闭集原型模型完成本地分类；
5. 可选用本地 Transformers 或 OpenAI-compatible API 对候选进行模型复核。

v1.1 进一步支持正式 `*_data/` 目录、严格八城市七来源清单、受控内存流式检测、Counter/State 指标语义、FRR 去噪、并发城市事件拆分、事件缓存和 API 并行推理。v1.2 将指标划分为故障触发、辅助证据和调度上下文，增加触发可靠性门控、20 分钟峰值抑制、事件城市候选约束与分类去偏。正式数据自动选择 `streaming`；公开样例继续使用内存模式保持回归稳定。

v1.3 修复了吞吐语义遮蔽、support 挤占 trigger、state 正常值未生效和 RCA 证据角色等问题，并加入语义量程、30 点异常基线冻结、因果族持续性、实现指纹、证据缓存和带分母的诊断日志。工作量 QPS 与滞后的 load average 只作辅助证据；分类按分钟去重相关信号，并以根因节点的主要连续触发段细化时间边界。`local` 后端适合本机开发和快速回归；正式提交建议启用 LLM 混合路径，在统计证据和图特征基础上完成最终复核。

v1.4 针对正式数据暴露的证据质量问题继续收敛：服务成功率和错误率使用原始 Counter 增量并进行可配置的贝叶斯收缩；低量程 `disk_io_util` 只保留为辅助证据；CPU 的低语义异常需要更强持续性；跨城市切分只允许 trigger 关系建立连接，并在切分后重新执行 trigger 准入、能量阈值与 confidence 计算。该版本不按目标事件数量删减结果，也没有设置统一的最小请求量或最小故障时长。

详细设计和实施计划见：

- `docs/requirements/AIOPS_OnePage.md`
- `docs/design/multisource-hybrid-model-design.md`
- `docs/plans/multisource-hybrid-model-v1.md`
- `docs/superpowers/specs/2026-09-14-multisource-v1-1-design.md`
- `docs/superpowers/plans/2026-09-14-multisource-v1-1.md`
- `docs/superpowers/specs/2026-09-14-formal-noise-calibration-design.md`
- `docs/superpowers/plans/2026-09-14-formal-noise-calibration.md`
- `docs/data/model-data-coordination.md`
- `docs/deployment/server-inference.md`

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## 样例数据

`sample/` 提供 3 个可直接运行的样例 case，每个 case 包含 8 个城市区域的多源网络观测数据。

本地混合模型只使用 Python 标准库，不要求安装 Torch。仅在运行本地 LLM 时安装：

```bash
python -m pip install -e ".[llm]"
```

## 运行 Baseline

### 本地多源模型（建议先运行）

```bash
python tools/run_sample_baseline.py \
  --output outputs/hybrid_v1_predictions.jsonl \
  --inference-log outputs/hybrid_v1_inference.json
```

命令会依次运行三个公开 Case。`hybrid_v1_predictions.jsonl` 是符合官方 Schema 的预测；诊断日志记录数据覆盖、坏行、事件能量、候选特征和分类 Top3，不保存 API Key 或原始 NetFlow 五元组。

运行单个目录：

```bash
python baseline/bian/run.py \
  --data-root sample/case_001 \
  --output outputs/case_001_predictions.jsonl \
  --inference-log outputs/case_001_inference.json
```

默认参数是 `--detector robust --decision-backend local`。旧的检测器仍可用 `--detector five-sigma` 回归验证。

正式第一批数据建议先运行纯本地流式阶段：

```bash
python baseline/bian/run.py \
  --data-root data/stage1/regions \
  --scratch-dir data/stage1/scratch \
  --evidence-cache outputs/stage1_evidence_v1_4.json \
  --event-cache outputs/stage1_events_v1_4.json \
  --output outputs/stage1_local_predictions.jsonl \
  --inference-log outputs/stage1_local_inference.json
```

正式目录默认严格检查八城市七来源；单城市调试需显式使用 `--allow-partial-input`。服务器与事件缓存用法见 `docs/deployment/server-inference.md`，数据协作要求见 `docs/data/model-data-coordination.md`。

### 四卡本地 Transformers 后端

本仓库提供基于 BiAn 方法实现的参考 Baseline。BiAn 方法来源于论文 [Towards LLM-Based Failure Localization in Production-Scale Networks](https://doi.org/10.1145/3718958.3750505)。

本实现结合 AIOps Challenge 2026 的数据组织方式、任务接口和结果输出进行了相应适配。为便于参赛者理解赛题数据的使用方式，并展示从多源数据读取、分析，到根因定位、故障分类、结构化结果输出及本地评测的完整流程，公开参考实现对原始方法进行了适当简化，并采用轻量级 7B 模型以降低运行资源要求。该 Baseline 主要用于展示完整的数据处理与评测流程，供参赛者参考，不代表 BiAn 方法的完整实现或最佳性能，也并非针对本赛题进行性能优化。

服务器准备好模型权重后，可使用四张 5090 自动分片：

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3 python baseline/bian/run.py \
  --data-root <FORMAL_DATA_ROOT> \
  --decision-backend transformers \
  --model <MODEL_PATH_OR_ID> \
  --output outputs/predictions.jsonl \
  --inference-log outputs/inference.json
```

模型参数可使用本地权重目录或兼容的模型 ID；加载器在 CUDA 可用时使用 BF16 和 `device_map="auto"`。旧参数 `--use-llm` 等价于从默认 `local` 切换到 `transformers`。

### OpenAI-compatible API 后端

接口已经预留，密钥只从指定环境变量读取：

```bash
export AIOPS_LLM_API_KEY='<YOUR_KEY>'
export AIOPS_LLM_API_BASE='https://example.com/v1'
python baseline/bian/run.py \
  --data-root <DATA_ROOT> \
  --decision-backend api \
  --api-key-env AIOPS_LLM_API_KEY \
  --model <API_MODEL_NAME> \
  --output outputs/predictions.jsonl
```

API 必须兼容 Chat Completions 的 `/chat/completions` 请求和 JSON 响应。不要把密钥写入配置、源码或推理日志。

## 本地评测

仓库提供本地评测工具，可用于验证预测结果：

```bash
python -m aiops_challenge_2026.evaluator \
  --ground-truth sample/ground_truth.jsonl \
  --predictions outputs/hybrid_v1_predictions.jsonl \
  --report outputs/hybrid_v1_evaluator_report.json
```

`examples/predictions.jsonl` 可用于演示评测工具的使用方式，不代表 Baseline 性能。

v1.4 在三个公开样例上的本地评测为 `Total=96.011111`、`AD=36.011111`、`RCA=40`、`Major=10`、`Minor=10`，3 个根因 Top1 和细分类全部正确。成都正式数据由 v1.3 的 167 条事件降至 104 条，无 trigger 事件为 0；被审计脚本判为“极小样本 ratio 驱动且被 30 分钟窗口推翻”的事件由 68 条降至 1 条。剩余 1 条同时存在连续三分钟的原始错误计数 trigger，不能仅依据 ratio 审计结果直接删除。八城市 v1.4 全量结果仍需重新扫描原始 CSV 验证，且这些无标签结果不代表正式隐藏数据成绩。

本次代表性输出已归档在 `artifacts/public-sample-v1/`。

时间戳必须包含时区信息，推荐统一使用 UTC。

## 目录结构

```text
aiops_challenge_2026/  公开配置、数据读取与评测工具
baseline/bian/         BiAn 参考 Baseline
baseline/bian/config/model_v1.json  第一版检测、融合和原型配置
examples/              预测结果示例
sample/                公开样例数据及参考答案
tools/                 Baseline 运行工具
tests/                 单元测试与公开样例回归测试
artifacts/             可复现的代表性预测与评测报告
```

## 开发验证

```bash
python -m unittest discover -s tests -v
python -m compileall -q baseline aiops_challenge_2026 tools
```

本地后端不读取 `sample/ground_truth.jsonl`。Ground Truth 只应作为推理完成后的独立评测输入。

## 参考文献

Wang C, Zhang X, Lu R, et al. Towards LLM-Based Failure Localization in Production-Scale Networks[C]//Proceedings of the ACM SIGCOMM 2025 Conference. 2025: 496-511.

论文链接：https://doi.org/10.1145/3718958.3750505

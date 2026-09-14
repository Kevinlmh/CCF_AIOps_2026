# AIOps Challenge 2026

本仓库提供 AIOps Challenge 2026 的样例数据、评测工具和参考 Baseline。

## 第一版多源混合模型

当前分支已在原始 BiAn Baseline 上加入可直接运行的第一版混合模型：

1. 统一读取 node、interface、routing、scrape health、traffic flow、NetFlow、FRR syslog 七类数据；
2. 用滚动中位数与 MAD 完成无监督异常检测，并按赛事 1～30 分钟先验切分事件；
3. 融合异常强度、持续性、时间先后、来源多样性、直接性和公开拓扑完成 RCA Top5；
4. 用覆盖官方 28 类故障的闭集原型模型完成本地分类；
5. 可选用本地 Transformers 或 OpenAI-compatible API 对候选进行模型复核。

`local` 后端适合本机开发和快速回归。正式提交建议启用 LLM 混合路径，从而让模型在统计证据和图特征基础上完成最终复核，而不是把纯规则脚本作为最终方案。

详细设计和实施计划见：

- `docs/requirements/AIOPS_OnePage.md`
- `docs/design/multisource-hybrid-model-design.md`
- `docs/plans/multisource-hybrid-model-v1.md`

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
python baseline/bian/run.py \
  --data-root <DATA_ROOT> \
  --decision-backend api \
  --api-base https://example.com/v1 \
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

当前第一版在三个公开样例上的本地评测为 `Total=98.855556`、`AD=38.855556`、`RCA=40`、`Major=10`、`Minor=10`。这只是公开样例回归结果，不代表正式隐藏数据成绩；正式数据需要继续校准长时间窗口、不同故障族和 LLM 后端。

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

# v3 Mac Deterministic Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Mac 上完成无需 LLM 权重的七源特征输入、事件检测、证据追踪、根因 Top-5、官方分类、合法提交及可选服务器诊断结果导入。

**Architecture:** `aiops_v3` 读取本机已有的七源特征库并可从公开原始样例构建同格式最小特征库。检测只看观测掩码和可解释变化，随后固定事件与候选集；规则诊断默认产出结果，服务器 JSONL 只能在通过同一候选/证据/分类契约校验后替换诊断。输出与审计分离。

**Tech Stack:** Python 3.13、NumPy、pytest；Mac 阶段不安装模型推理库、不下载权重。

**Spec:** `docs/superpowers/specs/2026-09-29-v3-local-llm-diagnosis-design.md`

## Global Constraints

- 第一批原始数据 104 GiB，不复制；已有七源特征库用 `np.load(..., mmap_mode="r")` 读取。
- `node_mask`、`edge_mask`、`log_mask` 的缺失值绝不当零值证据。
- 80 个官方网元及 28 个合法类别由本项目内官方配置校验；Top-5 必须去重且连续排名。
- 默认 `rules` 模式不得导入 torch/transformers 或访问网络；LLM 权重只在服务器下载。
- 公开三例用于契约及回归，不据此宣称全量准确率；第二批不存在时不生成完整竞赛分数。

## Review Focus

- `traffic-vm` / `monitor-vm` 缺观测：候选不能因为缺失值被提高。
- 累积计数器复位：原始样例构建时不得制造负的请求率。
- 同一事件多源重复触发：输出一条事件，并保留每条证据来源。
- 服务器响应的非法网元、类别、证据 ID 或重复候选：逐事件退回规则诊断。
- 先后两批合并：预测 ID 唯一、时间有时区、合法 Top-5 和分类。

---

### Task 1: 官方契约与包入口

**Files:** `aiops_v3/config/*.json`、`aiops_v3/contracts.py`、`tests/v3/test_contracts.py`。

**Interfaces:** `load_contract() -> OfficialContract`；`validate_prediction(record, contract) -> None`；`parse_time(str) -> datetime`。

- [ ] 写合法与非法 Top-5、类别、时间测试并确认先失败。
- [ ] 复制官方静态配置到 v3 包，实作严格校验与时间解析。
- [ ] 运行 `pytest -q tests/v3/test_contracts.py`，确认通过后提交。

### Task 2: 七源特征输入及轻量样例构建

**Files:** `aiops_v3/store.py`、`aiops_v3/raw.py`、`tests/v3/test_store.py`、`tests/v3/test_raw.py`。

**Interfaces:** `open_store(path: Path) -> FeatureStore`；`build_sample_store(raw_root: Path, destination: Path) -> Path`；`FeatureStore` 提供 `manifest`、`node_values/mask`、`edge_values/mask`、`log_values/mask`。

- [ ] 写内存映射读取、形状/清单不一致、缺失掩码和七源样例文件识别测试并确认先失败。
- [ ] 实作对已有特征库的只读适配；样例原始构建仅写必要特征且逐文件审计，保留计数器复位与观测状态。
- [ ] 运行任务测试并在公开样例上构建一次，核对七源行数和合理体积后提交。

### Task 3: 可追溯事件检测

**Files:** `aiops_v3/detection.py`、`tests/v3/test_detection.py`。

**Interfaces:** `detect(store: FeatureStore, settings: DetectorSettings) -> list[Event]`，每个 `Event` 含时间边界与证据 ID。

- [ ] 写持续突变、缺失值、同源去重、跨实体症状和边界测试并确认先失败。
- [ ] 实作节点直接证据、业务质量症状、FRR/NetFlow 辅助分量和时间合并；记录拒绝原因。
- [ ] 运行任务测试并在公开样例上核对事件时间覆盖，提交。

### Task 4: 根因候选、规则分类与证据包

**Files:** `aiops_v3/diagnosis.py`、`tests/v3/test_diagnosis.py`。

**Interfaces:** `build_evidence(store, event) -> EvidencePack`；`diagnose_rules(pack, contract) -> Diagnosis`。

- [ ] 写合法候选、直接证据优先、症状不冒充根因、资源/链路/路由/服务类别组合测试并确认先失败。
- [ ] 实作 8–12 候选的有依据召回、Top-5 排序、官方分类与证据 ID；所有假设性拓扑关系明确标注未知。
- [ ] 运行任务测试和公开三例回归，提交。

### Task 5: 可选服务器响应、输出与运行审计

**Files:** `aiops_v3/optional_llm.py`、`aiops_v3/output.py`、`aiops_v3/run.py`、`tests/v3/test_run.py`。

**Interfaces:** `load_diagnoses(path) -> dict[str, dict]`；`choose_diagnosis(event, rules, response, pack, contract) -> Diagnosis`；`run(input_store, output_dir, mode='rules', llm_responses=None) -> RunSummary`。

- [ ] 写默认无 LLM、合法服务器响应、非法响应回退、JSONL 规范和运行审计测试并确认先失败。
- [ ] 实作 `rules`/`llm-jsonl` 两种模式、证据包导出、官方预测 JSONL、机器审计和输入版本清单。
- [ ] 运行任务测试、全套测试、公开样例及第一批现有特征库干跑；核对产物大小和合法性，提交。

### Task 6: 使用说明和服务器交接

**Files:** `README.md`、`.gitignore`、`docs/v3-server-llm-handoff.md`。

**Interfaces:** CLI 输入输出示例、服务器诊断 JSONL 模板、模型版本记录字段。

- [ ] 写明 Mac 无权重运行、七源输入、公开样例和第一批命令、第二批接入方式、官方校验与已知限制。
- [ ] 创建 v3 专属忽略规则保护本机数据与模型权重。
- [ ] 核对文档命令可执行、`git status` 不出现原始数据、最终提交。

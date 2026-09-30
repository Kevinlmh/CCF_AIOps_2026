# CCF AIOps v3

v3 在 Mac 上默认运行完整的无模型权重流程：七源数据特征、事件检测、根因 Top-5、官方故障类别、预测 JSONL 和逐事件证据。服务器上的 LLM 是可选诊断器，只能对已确定的事件和候选重新排序与分类。

赛题约束见 [AIOPS_OnePage.md](docs/requirements/AIOPS_OnePage.md)，设计与已知局限见 [v3 方案](docs/superpowers/specs/2026-09-29-v3-local-llm-diagnosis-design.md)。OnePage 中旧版“数据/模型”人员分工不适用于 v3；v3 由一条完整流程维护。公开三个样例仅用于契约回归；当前未取得第二批数据，不能据第一批结果宣称完整竞赛成绩。

## 安装与运行

```bash
python -m pip install -e '.[test]'
python -m pytest -q

# 直接读取本机已有的七源特征库，不复制 104 GiB 原始数据。
python -m aiops_v3.run \
  --input-store data/feature_store/v2/stage1_all_cities_v2_20260926 \
  --output-dir outputs/v3/stage1_rules

# 可选保守检测对照：筛除短时、低强度、单设备、仅 CPU 的候选；
# 标准版仍是默认配置，保守版是否提升隐藏集得分需要提交验证。
python -m aiops_v3.run \
  --input-store data/feature_store/v2/stage1_all_cities_v2_20260926 \
  --output-dir outputs/v3/stage1_rules_conservative \
  --detector-profile conservative

# 独立对照开关：按请求量降低低样本业务症状权重，或让单探针来源网元进入 Top-5。
# 两项都尚未经过第一批隐藏标签验证，可分别与保守版比较。
python -m aiops_v3.run \
  --input-store data/feature_store/v2/stage1_all_cities_v2_20260926 \
  --output-dir outputs/v3/stage1_request_aware \
  --detector-profile conservative \
  --request-aware-service

# 也可从公开原始 CSV 构建 v3 特征库后运行。
python -m aiops_v3.run \
  --raw-root sample/case_001 \
  --build-store-to outputs/v3/sample_store \
  --output-dir outputs/v3/sample_rules
```

`--raw-root` 同样识别第一批 `data/stage1/regions/*_data/` 布局；第一批已存在经核查的七源特征库，Mac 上优先复用该库以节省时间与磁盘。第二批取得后可按相同方式构建或接入特征库，分别预测，再合并：

```bash
python -m aiops_v3.merge \
  --input outputs/v3/stage1_rules/predictions.jsonl \
  --input outputs/v3/stage2_rules/predictions.jsonl \
  --output outputs/v3/combined_predictions.jsonl
```

每次运行输出 `predictions.jsonl`、`evidence.jsonl`、`audit.jsonl` 和 `run_manifest.json`。输出目录须为空；原始数据、运行产物和模型权重不会纳入 Git。

有公开真值时可单独评测；没有第一批完整真值时不要用公开三例推断第一批分数：

```bash
python -m aiops_v3.evaluation \
  --ground-truth sample/ground_truth.jsonl \
  --predictions outputs/v3/sample_rules/predictions.jsonl \
  --report outputs/v3/sample_rules/evaluation.json
```

## 可选服务器诊断

服务器读取 `evidence.jsonl`，根据其中的 `candidates` 和 `signals` 生成响应 JSONL。Mac 端导入：

```bash
python -m aiops_v3.run \
  --input-store data/feature_store/v2/stage1_all_cities_v2_20260926 \
  --output-dir outputs/v3/stage1_server_diagnosis \
  --diagnoser llm-jsonl \
  --llm-responses outputs/v3/server_responses.jsonl
```

非法、重复、过期或缺失响应逐事件退回规则诊断，原因写入 `audit.jsonl`。服务器须回传同一行证据包的 `evidence_sha256`。响应格式与验证规则见 [服务器交接说明](docs/v3-server-llm-handoff.md)。Mac 无须安装或下载 LLM。

## 已知边界

当前候选检索只使用直接设备证据、目标城市症状和同城上下文。接口对端与服务域名到具体实例缺少权威映射，服务类事件的 Top-1 因而可能只是低证据候选；审计中的候选原因会明确标注。证据 ID 定位到分钟特征单元，现有 v2 特征库没有原始 CSV 行号；需要逐行来源时须重新构建带行号索引的特征库。第一批没有公开全量真值，标准配置的 375 条和保守配置的 316 条候选都不能视为准确率或最终分数；与用户确认的官方第一批 292 条之差及本轮实验见[检测审计](docs/v3-detection-feature-audit-2026-09-29.md)。第二批尚未下载，原始第二批构建与完整提交还未验证。

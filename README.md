# CCF AIOps 2026 · v3

v3 从两批特征库读取多源观测，完成故障事件检测、根因 Top-5 排序和官方类别输出。规则链路可在 Mac 独立运行；服务器 LLM 只在已检出的事件及候选网元内重排和分类。

赛题字段与数据约束见 [需求摘要](docs/requirements/AIOPS_OnePage.md)，当前数据、得分和已知限制见 [v3 当前状态](docs/v3-current-state.md)。

## 当前结果

根目录 `result.jsonl` 是两批路由上下文实验版，397 条；原保守版保存在 `outputs/v3/stage1_stage2_canonical_conservative_20261001.jsonl`，也是 397 条。两版官网反馈均为 **22.78450382836741**。实验版只改变 28 条事件的根因 Top-5 后续名次，事件时间、Top-1 和故障类别不变。

`outputs/v3/` 保留了这两份已评分合并文件、两批各自的预测/证据/审计、公开样例评测，以及可选服务器交接包。中间消融实验和旧版产物已清理。

## 安装与验证

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

公开样例真值在 `sample/ground_truth.jsonl`。完整隐藏标签不可用，公开样例分数不能代表正式得分。

## 从现有特征库重新推理

两批特征库分别位于 `data/feature_store/v3/stage1_canonical_20260930` 和 `data/feature_store/v3/stage2_canonical_20261001`。下面的命令复现当前路由上下文实验配置，输出到**新目录**；运行器不会覆盖已有目录或根目录 `result.jsonl`。

```bash
python -m aiops_v3.run \
  --input-store data/feature_store/v3/stage1_canonical_20260930 \
  --output-dir outputs/v3/rebuild_stage1 \
  --detector-profile conservative \
  --routing-dimension-detection --routing-context-candidates --temporal-context

python -m aiops_v3.run \
  --input-store data/feature_store/v3/stage2_canonical_20261001 \
  --output-dir outputs/v3/rebuild_stage2 \
  --detector-profile conservative \
  --routing-dimension-detection --routing-context-candidates --temporal-context

python -m aiops_v3.merge \
  --input outputs/v3/rebuild_stage1/predictions.jsonl \
  --input outputs/v3/rebuild_stage2/predictions.jsonl \
  --output outputs/v3/rebuild_combined.jsonl
```

只复现原保守版时，分别去掉三个路由/时序开关，再合并。每次运行产生 `predictions.jsonl`、`evidence.jsonl`、`audit.jsonl` 和包含输入、代码及输出哈希的 `run_manifest.json`。`merge` 会校验官方格式并重新编号预测 ID。

第二批原始 CSV 尚在本机时，可用 `python -m aiops_v3.build_features --raw-root data/stage2/regions --profile stage2 --preflight-only` 预检。重建特征库的完整参数见 `python -m aiops_v3.build_features --help`；目标目录必须不存在。

## 数据侧阈值审计

`data_side/results/stage1_threshold_audit_20261002.json` 和 `stage2_threshold_audit_20261002.json` 分别绑定两批特征库的实际输入。第二批报告同时对照第一批，记录每条节点规则的覆盖率、数值分位数、触发频率，以及优先复核的设备和时间窗口。新批次可运行：

```bash
python -m data_side.threshold_audit \
  --input-store data/feature_store/v3/stage2_canonical_20261001 \
  --reference-store data/feature_store/v3/stage1_canonical_20260930 \
  --output data_side/results/new_stage2_audit.json
```

这两份报告均为 `audit_only`：没有已确认的正常和故障窗口，分布变化只标记为待复核，不自动调整阈值。推理时可传入 `--calibration-profile data_side/results/stage2_threshold_audit_20261002.json`；运行器会核对特征库哈希并记录配置哈希。只有显式标记为 `validated`、登记正常及故障复核窗口数的配置，才允许使用 `rule_overrides` 按指标覆盖 `score_threshold`、`floor` 或 `absolute`；窗口内容仍需人工核实。现有两批审计配置不含覆盖值，因此复跑预测与已评分版本一致。详细结果见 [当前状态](docs/v3-current-state.md)。

## 可选服务器 LLM

当前本地检测链路不需要模型权重。已冻结的源码和两批保守版证据位于 `outputs/v3/server_bundle_20261002/`，服务器运行、响应回导及哈希校验见 [服务器交接说明](docs/v3-server-llm-handoff.md)。LLM 不能补漏检事件或更改事件时间。当前尚无服务器 GPU 实测结果。

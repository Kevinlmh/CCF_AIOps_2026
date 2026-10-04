# CCF AIOps 2026 · v3

v3 从两批特征库读取多源观测，完成故障事件检测、根因 Top-5 排序和官方类别输出。规则链路可在 Mac 独立运行；服务器 LLM 只在已检出的事件及候选网元内重排和分类。

赛题字段与数据约束见 [需求摘要](docs/requirements/AIOPS_OnePage.md)，当前数据、得分和已知限制见 [v3 当前状态](docs/v3-current-state.md)。

## 当前结果

根目录 `result.jsonl` 当前为两批合并的 **395 条** BGP 双端合并版，官网反馈 **22.789581578083233/60**。此前 397 条路由上下文版保存在 `outputs/v3/stage1_stage2_routing_experimental_20261002.jsonl`，得分 22.78450382836741；原保守版 `outputs/v3/stage1_stage2_canonical_conservative_20261001.jsonl` 同为 397 条及 22.78450382836741。新版本仅合并第一批两组同窗 BGP 事件，第二批保持 76 条。

`outputs/v3/` 保留了已评分合并文件、两批各自的预测/证据/审计、公开样例评测，以及可选服务器交接包。

[第二批覆盖审查与 BGP 双端合并实验](docs/v3-stage2-coverage-and-bgp-ablation-2026-10-03.md)记录了 395 条版本的单变量差异、官方反馈及第二批短 CPU 事件的离线消融。

## 安装与验证

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

公开样例真值在 `sample/ground_truth.jsonl`。完整隐藏标签不可用，公开样例分数不能代表正式得分。

## 从现有特征库重新推理

两批特征库分别位于 `data/feature_store/v3/stage1_canonical_20260930` 和 `data/feature_store/v3/stage2_canonical_20261001`。下面的命令复现当前已评分 395 条配置，输出到**新目录**；运行器不会覆盖已有目录或根目录 `result.jsonl`。

```bash
python -m aiops_v3.run \
  --input-store data/feature_store/v3/stage1_canonical_20260930 \
  --output-dir outputs/v3/rebuild_stage1 \
  --detector-profile conservative \
  --routing-dimension-detection --routing-context-candidates --temporal-context \
  --exact-bgp-session-merge

python -m aiops_v3.run \
  --input-store data/feature_store/v3/stage2_canonical_20261001 \
  --output-dir outputs/v3/rebuild_stage2 \
  --detector-profile conservative \
  --routing-dimension-detection --routing-context-candidates --temporal-context

python -m aiops_v3.merge \
  --input outputs/v3/rebuild_stage1/predictions.jsonl \
  --input outputs/v3/rebuild_stage2/predictions.jsonl \
  --output outputs/v3/rebuild_combined.jsonl

python -m aiops_v3.prediction_audit \
  --stage1-run outputs/v3/rebuild_stage1 \
  --stage2-run outputs/v3/rebuild_stage2 \
  --merged outputs/v3/rebuild_combined.jsonl \
  --report outputs/v3/rebuild_audit.json
```

复现此前 397 条路由上下文版时，仅去掉第一批的 `--exact-bgp-session-merge`；复现原保守版时，分别去掉三个路由/时序开关和该 BGP 合并开关，再合并。每次运行产生 `predictions.jsonl`、`evidence.jsonl`、`audit.jsonl` 和包含输入、代码及输出哈希的 `run_manifest.json`。`merge` 会校验官方格式并重新编号预测 ID。

`prediction_audit` 核对两批输入、代码和输出哈希，逐行验证预测与证据、审计的对应关系，并重演合并结果；同类重叠事件只列为复核线索，不自动删除。完整审查结论见 [两批复跑审计](docs/v3-two-batch-audit-2026-10-03.md)。

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

## 特征库序列与关系审计

`data_side.store_evidence_audit` 从现有 v3 特征库读取观测掩码、维度侧车和边登记，输出 `series_gaps.csv`、`counter_audit.csv`、`entity_relations.csv` 与 `summary.json`。已生成的两批结果分别在 `data_side/results/store_evidence_stage1_20261002/` 和 `store_evidence_stage2_20261002/`。新特征库可运行：

```bash
python -m data_side.store_evidence_audit \
  --input-store data/feature_store/v3/stage2_canonical_20261001 \
  --output-dir data_side/results/new_store_evidence_audit
```

缺口只统计单条序列首次和末次观测之间的未观测分钟，不把零值当成缺失，也不把稀疏业务探针自动判为故障。计数器表只比较相邻已观测分钟中的原始累计序列；业务流原始累计值入库前已转换为增量，审计仅能读取解析器保留的 `counter_reset` 信号，汇总将两种序列分别计数。关系表记录接口归属、路由地址引用、业务探针目标和 NetFlow 观测接口的来源字段；接口 ID 与特征库边使用同一规范化形式。它不证明物理链路对端或域名对应的具体服务实例。这些产物只供数据与证据复核，不参与预测生成。

## 可选服务器 LLM

当前本地检测链路不需要模型权重。已冻结的源码和两批保守版证据位于 `outputs/v3/server_bundle_20261002/`，服务器运行、响应回导及哈希校验见 [服务器交接说明](docs/v3-server-llm-handoff.md)。LLM 不能补漏检事件或更改事件时间。当前尚无服务器 GPU 实测结果。

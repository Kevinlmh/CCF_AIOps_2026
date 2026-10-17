# v4 LLM 角色层修复与三批重跑（2026-10-17）

本文件只记录本轮在**不重新设计数据处理、状态发现和证据层**前提下的修改、测试与运行方式。
数据层、状态层、证据层、评测器和原始 CSV 均未改动。

## 1. 修改依据

审查已验证两类问题：

1. **类别与证据脱节**：`pred_000090` 用 `beida-traffic-vm` 的认证业务记录支持
   `wuhan-service-vm-1` 的 `resource/cpu_pressure`，复核 accept、严格导出仍放行。
   原实现只要求“类别是官方配对”，不检查所选类别与已交付机制是否一致。
2. **弃权直接丢弃事件**：`diagnose_event` 要求定位与分类同时 resolved，复核再要求 accept。
   已确认事件里，分类或定位弃权、以及复核 defer 的记录不会导出，等于放弃该事件的
   AD/RCA/大类/小类全部分数。第一批 828 个已确认事件中只有 466 条被导出。

此外，状态包已包含 `stat_score / cluster_id / cluster_distance / drivers /
feature_coverage / reference_status / state_changed` 等字段，但角色投影把它们全部丢弃；
角色只能看到原值和规则输入，无法比较统计与聚类偏离。

## 2. 已修改内容（全部在 `aiops_v4/agents/`、`aiops_v4/experiments/`、类别配置）

| 项 | 位置 | 行为 |
|---|---|---|
| 证据先验模块 | `agents/evidence_priors.py`（新增） | 只用实际交付的 state `drivers`、`rule_signals`、`feature_values`、`dimensions` 推导最强机制；不读取标签、不补造观测 |
| 类别纠正 | `agents/export.py` | 模型类别若与交付证据不一致，改用证据支持的类别，并记录 `category_adjustment`（原值/新值/依据） |
| 弃权回退 | `agents/export.py`、`agents/pipeline.py` | 新增 `export_abstentions`：定位或分类至少一方 resolved 的已确认事件，用已交付证据补齐另一方后导出；两侧都弃权或复核 reject 时不导出 |
| 统计/聚类上下文 | `agents/tools.py` | 紧凑观测新增统计分、簇、drivers、覆盖、参考状态等字段；drivers 截断为 8 项 |
| 状态组过滤 | `agents/structured.py` | `query_states.group_id` 限定为已交付状态组，避免把 bundle_id 当 group_id |
| 第三批新增类别 | `aiops_challenge_2026/config/fault_taxonomy.json`、`aiops_v4/experiments/knowledge.py` | 增加 `link/ndp_failure`、`link/vxlan_misconfig`、`firewall/mtu_blackhole`，共 35 类 |
| 分片运行 | `agents/pipeline.py`、`agents/__main__.py` | `--shard-index/--shard-count` 按 bundle 哈希分片；`--export-abstentions`；`--evidence-wire`、`--temperature`、`--max-prompt-chars` 可配置 |
| 依赖校验开关 | `evidence/index.py` | `AIOPS_SKIP_LAYER_VERIFY=1` 跳过每个 worker 启动时对数十 GB 上游层的重复摘要；运行前后仍核对证据库 SHA |

**未改动**：原始数据解析、窗口、状态发现、候选准入、证据包构建、评测器、提交格式。
LLM 仍然只做确认、定位、分类、复核四个角色；没有增加工具轮数或角色数量。

## 3. 测试

- 服务器 `tests/v4/test_agent_*`、`test_structured_roles`、`test_component_review`、
  `test_evidence_first_roles`、`test_ranked5_rework`、`test_simple_role_protocol`、
  `test_fallback_export`、`test_evidence_priors`、`tests/shared` 等：**197 passed**。
- 服务器全量 `pytest -q`：**1013 passed，5 failed，11 errors**；失败项均为既有环境/产物问题
  （缺失 `wire.py.before` 冻结夹具、`outputs/` 下被 pytest 收集的调优脚本、实验评测夹具），
  与本次修改无关；角色与证据相关测试全部通过。
- 新增回归：
  - 业务证据不支持 CPU 时不得判 CPU；模型类别无证据时按交付机制纠正；
  - 复核 defer 在策略开启时导出、reject 永不导出；
  - 定位弃权时用交付设备回退；两侧都弃权不回退；
  - 紧凑观测保留统计/聚类/drivers；`query_states` 组过滤闭合。

## 4. 本轮运行

服务器 `outputs/v4/rerun_20261017/`：

- `controller.py`：按 stage2 → stage3（等待证据层）→ stage1 顺序运行，每批 4 个分片；
  `AIOPS_SKIP_LAYER_VERIFY=1`、`--evidence-wire v4-evidence-wire-3`、
  `--max-completion-tokens 2048`、`--max-prompt-chars 160000`、`--export-abstentions`。
- 模型为 `Qwen/Qwen3-8B-AWQ`，两个 vLLM 端点（8000/8001，各 TP=2）。
- 每批产出 `merged_<batch>.jsonl` 与 `merged_<batch>.summary.json`；
  `controller.log` 记录分片和合并事件。
- stage3 证据层完成后自动派生 `a1-readable-1` 交付视图并加封存，不改原始证据库。

运行日志在 `controller.out`、`stage*_shard_*.log`，分片结果在 `runs/<batch>/shard_*/`。
合并文件只做官方格式校验、唯一 ID、去重和重新编号，不修改任何预测内容。

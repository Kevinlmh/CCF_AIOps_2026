# v3 多源证据 + LLM 多角色优化结果（2026-10-18）

## 1. 范围与分支

- 服务器：`liminghang@121.48.161.203`，仓库 `~/work/CCF_AIOps_2026`，分支 `v3`。
- 代码基线：从 `5e69701`（Mac v3 分支）出发，新增 6 个提交；**已提交、未 push**：
  `1d1b52e`（第三批黑洞前缀守卫）→ `2258575`（计划+pytest 范围）→ `24968bd`（多源证据+四角色）→ `a7eab8d` → `177c095` → `918e624` → `c7504c0` → `c101a51`。
- Mac 本地分支 `v3` 与服务器同源，最新提交 `c101a51`。
- 测试：Mac 与服务器均 **232 passed**（`pytest -q`）。

## 2. 代码改动（新增/修改）

### 新增模块

| 文件 | 作用 |
|---|---|
| `aiops_v3/netflow_stats.py` | 从原始 5 元组 CSV 流式聚合 `分钟 × 服务 × 观察者城市` 的请求/响应包数，输出紧凑 JSONL + manifest；兼容带引号 CSV、BOM、CRLF、`\N` |
| `aiops_v3/netflow_detection.py` | 检测「全观察者无响应」（服务故障）与「单观察者无响应」（路径故障，要求 ≥3 分钟）；输出服务/路径信号 |
| `aiops_v3/classification.py` | 机制矩阵：类别族 ↔ 允许证据族 ↔ 根因设备类型；`is_clear_event` 快路径判定 |
| `aiops_v3/roles.py` | 四角色（确认/定位/分类/复核）JSON schema、prompt、语义校验器 |
| `aiops_v3/role_runner.py` | 四角色批量运行器：vLLM/OpenAI 协议、断点续跑（按 evidence/prompt/model hash）、逐角色回退、localize/classify 并行 |
| `aiops_v3/role_import.py` | 角色结果逐字段应用：定位→Top5、分类→类别、确认→边界；review 只作否决 |

### 修改模块

| 文件 | 改动 |
|---|---|
| `aiops_v3/detection.py` | `Event` 增加 `observers`；`DetectorSettings` 增加 `netflow_events/netflow_stats_path`；NetFlow 服务/路径事件与规则事件合并、去重、重新编号 |
| `aiops_v3/diagnosis.py` | `EvidencePack` 增加 `observers/topology/candidate_priors/evidence_owners/quality`；多观察者候选先验；合并事件保留规则直接证据；NetFlow 特征的类别映射 |
| `aiops_v3/output.py` | `prediction_record` 支持边界覆盖 |
| `aiops_v3/run.py` | 新参数 `--netflow-stats/--netflow-events/--diagnoser roles-jsonl/--role-results/--role-scope/--no-boundary-refine`；manifest 记录新哈希 |
| `aiops_v3/server_llm.py` | `_pack` 兼容新增证据字段 |

### 关键设计（与 v4 的差异）

- **LLM 不增删事件**：只允许改根因顺序、类别、±10 分钟内边界；事件数由规则+NetFlow 检测决定。
- **机制矩阵 + 证据归属**：引用证据必须属于被选网元，且特征族与类别匹配（v4 的「traffic 指标证明 CPU」会被拒）。
- **逐字段回退**：定位/分类/确认任一失败只回退该字段；review 无效时不否决已通过校验的字段。
- **批1 冻结**：批1 仍用原 319 条冻结预测，只有批2/批3 启用新通道与角色。

## 3. 数据处理（服务器）

原始 NetFlow（5 元组分钟级）只读扫描三个目标城市（DNS→beida、Web→shanghai、Auth→wuhan）：

| 批次 | 输入文件 | 原始大小 | 输出 | 行数 |
|---|---:|---:|---|---:|
| 批2 | 9（3 城市 × 3 时段） | 25.9 GB | `data/netflow_stats/stage2_service.jsonl`（33 MB） | 180,267 |
| 批3 | 15（3 城市 × 5 时段） | 53.1 GB | `data/netflow_stats/stage3_service.jsonl`（108 MB） | 604,076 |

原始 CSV 解压位置：`/data/liminghang/CCF_AIOps_2026/stage2_extract`、`stage3_raw`（保留，未删除）。
特征库：从 Mac 同步 `stage2_canonical_20261001`、`stage3_canonical_20261017`，manifest SHA-256 与 Mac 一致。

## 4. 预测结果

三批合计（官方契约逐行校验通过、ID 唯一、按月排序）：

| 文件 | 行数 | 月份分布 | 说明 |
|---|---:|---|---|
| `outputs/v3/opt_20261018/submission_S1.jsonl` | 580 | 2026-08:280 / 09:120 / 10:180 | 批1 冻结 + 批2/3 规则+NetFlow 事件 |
| `outputs/v3/opt_20261018/submission_S2.jsonl` | 580 | 同上 | S1 + 四角色应用 |

SHA-256：
- S1 `66853616e09c6e6c362a47b3d67c7a495ecc6b2cc0795329987437a5df8b15a5`
- S2 `b5ec0a7167e2e2de79ebf6e1b2c7b2c59fb731bd11c729a3151f1504b88b5f02`

### 批2 相对原 76 条的变化（S1）

81 条（+6 新增、-1 合并/替换），新增事件均来自 NetFlow 证据：

- 4 × `service/dns_down`（09-18 23:04、09-19 03:20、09-22 09:47、09-23 20:15，beida-service-vm-1）
- 1 × `service/auth_timeout`（09-23 04:54–05:19，wuhan-service-vm-1）
- 1 条 CPU 事件根因由 beida-service-vm-1 调整为 beida-br-1

### 批3 相对原 173 条的变化（S1）

180 条（+15 新增、-8 合并/替换），新增包括：

- 5 × `service/dns_down`（10-02 11:38、10-04 14:39、10-05 09:05、10-11 09:44、10-13 04:16）
- 1 × `service/auth_timeout`（10-13 02:56–03:16，20 分钟）
- 1 × `service/web_5xx`（10-13 18:18–18:22）
- 3 × `routing/bgp_session_down`（10-04、10-05、10-14）
- 其余为短 CPU 事件根因/窗口随合并调整

其中 10-02 11:39–11:44 的 DNS 全客户端无响应、10-13 02:56–03:15 的 auth 无响应，在改前均无对应预测。

### 四角色运行与影响（S2 相对 S1）

- 批2：81 事件，角色行 324（accepted 228 / fallback 96）；定位 accepted 76、分类 80、确认 42、复核 30。
- 批3：180 事件，角色行 705（accepted 454 / fallback 251）。
- **最终只有 6 条预测被角色层改动**（5 条根因、1 条类别），其余保持规则值：
  - 09-17 06:19 `disk_io_pressure → dns_down`
  - 09-20 15:05、10-03 10:52、10-10 13:06、10-14 01:43 根因由 br-1 调整为 cr-1/cr-2
  - 10-01 08:10 BGP 事件 Top1 由 shanghai-br-1 调整为 shenyang-br-1

角色层保守的原因：确认角色边界提案常有越界/时长问题（fallback 98），复核角色常 missing_upstream（fallback 126）；导入层按「逐字段回退 + review 只否决」处理，未强行放行。

## 5. 验证

- `pytest -q`：232 passed（Mac 与 121.48.161.203 均通过）。
- 三批合并文件逐行 `validate_prediction` 通过；ID 唯一；月份 280/120/180。
- 特征库 manifest SHA-256 与 Mac 一致；NetFlow stats 带输入文件 SHA/manifest。
- 公开三例回归未受影响（改动均在新开关下；批1 预测文件保持冻结）。

## 6. 提交建议（每天 5 次）

| 编号 | 内容 | 目的 |
|---|---|---|
| S0 | 原 `result.jsonl`（568 条） | 已有基线；若还没拿到批3 分数，先提交它 |
| S1 | `outputs/v3/opt_20261018/submission_S1.jsonl` | 单变量：NetFlow 证据通道 |
| S2 | `outputs/v3/opt_20261018/submission_S2.jsonl` | 单变量：四角色 |

建议顺序 S0 → S1 → S2，逐次记录总分；掉分立即回退上一版。**未做官方提交，也未使用隐藏标签。**

## 7. 限制与风险

- 无隐藏真值：新增的批2 +6 / 批3 +15 条是「有可复核 NetFlow 证据」的候选，不是已证实的准确率提升。
- 单观察者路径事件已从 1 分钟收紧到 ≥3 分钟，避免 21 条 1 分钟噪声事件；仍可能有误报。
- 角色层 accepted 率 64%–70%，confirm/review fallback 偏高；S2 与 S1 仅差 6 条，角色收益需官方分数验证。
- 批1 未改；若 S2 ≥ S1，可再考虑对批1 跑角色（计划 T11）。
- 服务器上 vLLM 已停止，GPU 已释放；原始 CSV 解压目录保留。

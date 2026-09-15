# 证据质量审计与修复建议

> 2026-09-15。基于 v1.3 成都单城运行的证据回溯。**所有结论都不依赖标签**——用序列自身在更大窗口下的行为来判定。

## 一、审计方法

`tools/audit_event_evidence.py` 回到原始 CSV 重算每条触发证据：

- **ratio 类**：按 `series_key` 找到原始行，重算窗口内 `requests_total` 增量得到真实样本量；再用 30 分钟窗口重算同一比值。若一分钟窗口极端而长窗口正常，则该事件由采样噪声开启。
- **disk 速率类**：载入该网元完整序列，计算零值比例。

```bash
python tools/audit_event_evidence.py \
  --inference-log outputs/v1_3/chengdu/final/chengdu_v1_3_inference.json \
  --data-root data/stage1/regions/chengdu_.../chengdu_..._data \
  --report outputs/v1_3/chengdu/evidence_audit.json
```

**建议将「被 30 分钟窗口推翻的事件数」纳入回归指标**，它比事件总数更能反映证据是否真实。

## 二、已确认问题

### 问题 A：ratio 类证据由极小样本算出（严重）

成都 v1.3 的 167 个事件中：

| 项 | 数量 |
|---|---|
| 由极小样本 ratio 开启（<30 请求） | **81（48.5%）** |
| 其中被 30 分钟窗口推翻 | **68（40.7%）** |
| 事件中至少含一条 | 115（68.9%） |

驱动点的窗口内请求数：中位 **6**，最小 1，最大 26。

被推翻的实例：

```
pred_000001:  1分钟   4 请求 → ratio=0.5       | 30分钟  529 请求 → ratio=0.9962
pred_000002:  1分钟   8 请求 → error_ratio=1.0 | 30分钟  488 请求 → ratio=0.0164
pred_000012:  1分钟   2 请求 → ratio=0.5       | 30分钟  367 请求 → ratio=0.9973
```

68 例的长窗口真实比值全部落在 1.0（36 例）或 0.0（32 例），即完全正常。

**连带后果**（比事件数更值钱）：

- **rank1 根因 79%（132/167）是 `traffic-vm`**。traffic 证据的 `node_id` 恒定是 `<源地域>-traffic-vm`，由 traffic 开启的事件必然指向它。
- **分类分布与官方类别表严重不符**：service 119（71.3%）、resource 42、routing 6、**link 0、firewall 0**。官方 28 类中 link+firewall 共 9 类（32% 的类别空间）完全未出现。

### 问题 B：disk 速率周期性归零（轻微）

| 项 | 数量 |
|---|---|
| 作为触发指标 | **0** |
| 事件中至少含一条 | 129（77.2%） |

这 588 个证据点**全部是 `support` 角色**——v1.2 已把 `disk_read_rate`/`disk_write_rate` 标为 support，因此**开不了事件**。对排序影响也很小：仅 1/167 事件的最强 support 证据是它，61/167 出现在 rank2 候选里。

**顺带发现**：`node.disk_read_rate` 在 br-1/br-2/cr-1/cr-2 上零值比例 **100%**，在 fw/service-vm-* 上 99.5~99.9%。该指标在成都数据里没有信息量，但仍配着 4096 的 `metric_scale_floors`。需数据侧确认是设计如此还是采集缺失。

## 三、修复建议

### 建议 1（最高优先级）：ratio 类证据的贝叶斯收缩

**不能用硬阈值。** 实测每分钟请求量：dns 中位 63、web 中位 60、**auth 中位仅 16**；全部数据中 <30 请求的分钟占 33.1%。硬卡 30 会砍掉三分之一的合法证据点。

**方案**：在 [`_iter_traffic_file`](../baseline/bian/preprocessing/multisource.py) 计算 `success_ratio` / `error_ratio` 处加伪计数：

```
adjusted = (numerator + α·p₀) / (requests + α)
```

`p₀` 取正常水平（success=1.0，error=0.0），α 为伪样本量。α=30 时：

| 场景 | 原始 | 收缩后 | 得分 | 结果 |
|---|---|---|---|---|
| 4 请求 2 成功 | 0.50 | 0.94 | 3.0 | 不再成为证据 |
| 16 请求 8 成功 | 0.50 | 0.83 | 8.7 | 仍为证据（削弱） |
| 16 请求 0 成功 | 0.00 | 0.65 | 17.4 | 强证据 |
| 500 请求 0 成功 | 0.00 | 0.06 | 25.0 | 强证据 |

`request_delta` 在调用点已有，无需改数据结构。建议把 α 做成配置项 `traffic_ratio_prior_weight`。

### 建议 2：error 类额外要求最小失败次数

收缩对 `success_ratio` 效果好，对 `error_ratio` 稍弱——「8 请求全失败」收缩后仍得 0.21。建议对 error 类要求最小绝对失败数（如 ≥5 次）才计为证据。

### 建议 3：先重跑，再判断是否要动证据归属

`traffic-vm` 霸榜 79% 可能是问题 A 的连带后果，也可能是结构性缺陷（traffic 证据的 `node_id` 归属规则）。**先做完建议 1 重跑，再看 rank1 分布**，避免过早改动 [`_traffic_related_nodes`](../baseline/bian/preprocessing/multisource.py)。

### 建议 4：`disk_read_rate` 交数据侧确认

该指标零值比例 100%，建议暂时从 `metric_scale_floors` 移除，避免误导。

### 建议 5：审计脚本纳入回归

每次调参后运行 `tools/audit_event_evidence.py`，以「被 30 分钟窗口推翻的事件数」作为质量指标。成都 v1.3 基线为 **68**；建议 1 完成后应大幅下降。

## 四、不依赖标签的判定方法

本轮的判定不依赖任何 ground truth：

- **长窗口推翻**：同一序列在更大样本下信号消失 → 采样噪声
- **零值比例**：指标自身历史中该值是否常见 → 正常波动
- **类别覆盖**：官方 28 类中整类缺失 → 结构性问题

这三条判据都可以固化进回归测试。

---

# 全量运行（八城 14 天）复核

> 2026-09-15。对 `outputs/v1_3/all_cities/stage1_local_inference.json`（635 事件）的复核。

```bash
python tools/audit_event_evidence.py \
  --inference-log outputs/v1_3/all_cities/stage1_local_inference.json \
  --data-root data/stage1/regions \
  --report outputs/v1_3/all_cities/evidence_audit.json
```

## 五、全量审计结果

| 项 | 数量 | 占 635 |
|---|---|---|
| 由极小样本 ratio 开启（<30 请求） | 245 | 38.6% |
| **其中被 30 分钟窗口推翻** | **237** | **37.3%** |
| 事件中至少含一条 | 442 | 69.6% |
| 切分后完全无 trigger | 11 | 1.7% |

驱动点的窗口内请求数：中位 **6**，最小 1，最大 28。

**两项修复叠加**：237（被推翻）+ 11（无 trigger）无交集，合计 248 条应拒绝 →
**635 → 387，为公开先验 292 的 1.3 倍。**

## 六、对既有诊断的三处补充

### 补充 1：跨城耦合的位置在切分之前（重要）

`split_concurrent_events` 的并查集确实无角色过滤（support 也能连接城市），但更根本的是
[`_energies`](../baseline/bian/anomaly_detector/robust_detector.py) **只按 `(分钟, 源)` 聚合，没有城市维度**：

```python
by_minute_source[_minute(point.timestamp)][point.source].append(point.score)
```

八城同一分钟的异常**在形成时间窗之前就已经合并成一个能量值**。切分是在事后拆，而不是从来没合过。
给 `_energies` 增加城市键可以从源头避免粘连。

### 补充 2：切分后 confidence 被原样继承

`split_concurrent_events` 中 `confidence=original.confidence`，子事件直接继承父事件置信度而不重算。
实测 11 条无 trigger 的事件里有 2 条 confidence = 0.916（封顶值），却只有 1 条 support 证据。

### 补充 3：语义量程从未参与触发决策

`node.disk_io_util` 触发 4078 次，其中 **98.3% 的 `semantic_score = 0`**（利用率低于危险区起点 70%），
全部靠相对偏离开事件。而同一族的 `disk_read_rate` / `disk_write_rate` 是 `support`，三者角色不一致。

量程语义目前只用于 RCA 的 severity 加权、分类加权和 `semantic_single_minute_threshold` 的单分钟放行，
**没有「量程不危险就不许开事件」的门槛**。

## 七、修复优先级（全量复核后）

```
① ratio 贝叶斯收缩 + error 最小失败数   → 消掉 237 条(37.3%)
② 切分后重新校验 trigger               → 消掉 11 条，并补上 confidence 重算
③ _energies 增加城市键                 → 从源头阻止跨城粘连
④ 有量程指标增加量程门槛               → 抑制 disk_io_util 类相对偏离触发
⑤ 事件拒绝机制                         → 应基于证据质量，不是数量目标
```

**关于 ⑤ 的取舍**：评分公式中 FP 惩罚封顶 12 分（`alpha_fp = 0.7 + 0.3×precision`），
而漏报是线性无上限的。因此**拒绝机制必须基于可证伪的证据质量（如②的长窗口推翻），
不能按目标数量截断**。


# 第三批 v3 前缀起点校验

## 问题与修正

第三批完整建库后，原版v3输出866条，其中695条blackhole。694条落在三个核心路由器的批次起始长期缺失区间内。原始CSV与维度侧车确认：同一分钟不同IPv6前缀的route_exists同时有1和0，节点级min聚合丢失了前缀身份；全批次中位数再把持续缺失判为异常，按30分钟反复切段。它不能证明694个独立故障。

新增可选 `--prefix-aware-blackhole`，默认关闭，保留原版行为。开启时读取既有维度侧车：只有同一前缀在相邻、连续可观测分钟发生存在→缺失，才允许将这一连续缺失段作为黑洞候选；批次起点缺失、新系列首次缺失、观测间断后的缺失记录为 `blackhole_onset_unobserved / unresolved_observation`，不认定为正常。

二值前缀先前的存在建立参考值1，避免另一个前缀的长期零值掩盖新发生的缺失。仍沿用原来的分数门槛、floor、最长30分钟区间及事件合并、根因排序和分类逻辑。CPU、内存、磁盘、BGP等规则没有修改。未引入隐藏标签或人工指定日期、网元及目标预测条数。

这是保守的起点准入：从批次开始就已发生的真实故障、跨观测缺口开始的真实故障也可能不再被单独检出。不能把此次预测数下降解释为已测得准确率提升。

## 运行与产物

复用 `data/feature_store/v3/stage3_canonical_20261017`，不重新解压或扫描88.9GB原始数据。

```sh
.venv/bin/python -m aiops_v3.run \
  --input-store data/feature_store/v3/stage3_canonical_20261017 \
  --output-dir outputs/v3/new_stage3_prefix_guard \
  --detector-profile conservative \
  --routing-dimension-detection --routing-context-candidates --temporal-context \
  --prefix-aware-blackhole
```

本次目录 `outputs/v3/stage3_20261017_prefix_guard/` 包含：

- `control_run/`：关闭新选项，原版866条，预测字节与调整前完全一致。
- `guarded_run/`：开启校验，173条，附证据、审计、输入/源码/输出哈希。
- `comparison.json`：逐事件比较；169条非黑洞预测及原有1条短时黑洞保持完全一致。原有两条OSPF区间19:30–20:00、20:00–20:30 UTC（10月2日）还原为19:55–20:21的连续26分钟候选。额外检出10月4日16:51–17:17两个有新前缀缺失起点的候选，原版已检出的10月10日黑洞候选保留；当前blackhole共3条。
- `prefix_onset_support.json`：保留的黑洞候选与同前缀先前存在观测的对应记录，补充节点级主证据包的前缀来源信息。
- `public_samples/`：三个公开样例预测不变，Total 96.011111，TP3/FP0/FN0；它们是既有开发样例，不是独立隐藏集验证。
- `stage3_predictions.jsonl`：修正版第三批173条。
- `stage1_stage2_stage3_predictions.jsonl`：历史395条加173条，共568条；根目录result.jsonl为相同副本。
- `verification-report.json`：最终哈希、官方契约、输入区间、证据一致性及历史395行未变的验证。

旧版原始产物保存在 `outputs/v3/stage3_20261017_control/`，未被覆盖。新特征库不作修改。

## 验证与边界

7个新增回归先失败，再经最小修正通过；全套119项通过。真实建库小样本覆盖：初始持续缺失、另一个前缀长期缺失时的新故障、观测间断、已观察起点的长事件、CPU独立保留、缺少前缀侧车及运行参数记录。独立只读审查没有发现阻断项；主证据包仍使用v3节点级Signal，当前交付通过单独的prefix_onset_support补充前缀身份及起点证据。

第一、二批已评分395条保留原文件和哈希，不按新规则重判。新文件只完成本地工程与证据核验，尚无官网新评分，也没有自动提交比赛平台。

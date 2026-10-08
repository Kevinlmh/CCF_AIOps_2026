# v4 第二步：字段语义与窗口特征

用户在确认“先做字段语义表与窗口特征、再接聚类”的顺序后要求继续编码。本步按已选 v4 架构实施，不再设置设计批准门槛。Python 3.10+，仅用标准库；保留已有 data/output/outputs/submit.py 文件，在当前 v4 分支顺序实现。

## 输入与语义

复用 aiops_v4.data.RawRecord，不修改第一步原始证据。公开语义登记表区分 gauge、counter、interval_count 和 unknown，包含单位、依据和 verified/inferred/unverified 状态；未知字段只保留自身原单位统计，不自动差分或换算。单位假设不可伪装为官方定义。

参考官网数据/规则页面（2026-10-08 浏览器实读）：https://challenge.aiops.cn/home/competition/2087843807868489822 。第二批按官方安排缺少详细业务流指标与 FRR 日志；仅共享通用代码和公开语义，不读取标签，不做跨批次学习。官网没有给出本次 CSV 的完整单位表或无时区时间定义。

无时区时间须由调用方显式给出 IANA 时区；aware 时间使用自身偏移。优先重新解析 record.raw 的原始时间，不盲用第一步 assumed_utc 结果。官方 SDK 的 UTC 约定及公开业务流 Unix 时间对齐支持 UTC 选择，但只是依据，保留解释来源和未获官方明确确认的状态。拒绝歧义/不存在的夏令时本地时间。

网元分为官方允许候选与辅助身份。probe-vm 等未知角色保留独立 auxiliary identity，不转换成 traffic-vm 或合法候选。

## 窗口与粒度

固定、非重叠 UTC 窗口，epoch 对齐，默认 60 秒，宽度可配置。边界采用 [start,end)。不补造空窗口，不填零。缺失率按已到达记录中适用字段计算；时间覆盖率只有显式给定 expected_step_seconds 时才计算，该步长是运行假设。

- resource：node，按网元分组。
- link：interface，按网元/interface_id 分组。
- routing：按网元/metric_name/完整 label 分组，保留 peer 等维度，不混合不同 peer 或指标。
- traffic：业务数据按原 series_key/源目标/类型等身份分组；NetFlow 按网元/interface_id/protocol 汇总分钟数量，原始五元组可由证据行号找回。接口/协议汇总不推断业务类别、目标 VM 或跨区实际路径。
- collection：scrape 按网元/target/exporter 分组，作为观测健康上下文。
- log_context：FRR 只提供事件计数和原始日志引用，后续 Agent 自行查询。

每个窗口输出 batch、稳定 ID、网元/角色/候选资格、view/source/dimensions、UTC 起止、原始记录数、数值特征/单位/语义依据、有效/缺失/坏值计数、时间覆盖、质量标记、有限原始引用及引用是否截断。

Gauge/unknown 输出均值、总体标准差、min/max、first/last、趋势斜率（仅 gauge）、有界分位数。Counter 输出原始水平统计及独立 delta/rate；按完整序列身份和时间排序，以后一个样本所在窗口归属。首样本、缺失/坏值、时间冲突、下降、超出 counter_max_gap_seconds（默认 180 秒）均不能产生可信差分；下降只记 counter_decrease，不假装知道重置前的数量。重复时间相同值只作一次采样，不同值均不进入值统计并打 conflict 标记。NetFlow 允许同一时间多条真实流记录，按字段 interval_count 求和，不差分；不把 flow_record_count 当成独立五元组数量。

## 有界离线处理与输出

CSV 可能乱序且文件可能按网元交错，使用临时 SQLite 对精简观测按 series/time 排序，流式计算单一序列窗口。内存不随全量行数或序列数增长；高基数原始地址不进入全局 Python 集合。每指标分位数蓄水池上限 128；每窗口引用上限 8。完整实体粒度与 schema 保存在 summary/semantics。

CLI：python -m aiops_v4.features build --root ... --batch ... --naive-timezone UTC --output-dir ...；支持 --window-seconds、--expected-step-seconds、--counter-max-gap-seconds、--max-rows-per-file。产物 windows.jsonl、summary.json、semantics.json、semantics.csv、rejected.jsonl、report.md。输出目录必须是新路径且在输入目录外；临时构建成功后才发布，summary 最后发布，失败不会伪装为可用结果。语法坏文件使构建失败；无法解析时间的记录保留于 rejected.jsonl 并计数，未删除原始记录。

prefix_sample 明确传播，不能视为完整批次；全量仅表述扫描完成，不等同完整模态或无缺失。新功能不调用 LLM、不生成故障判断，不使用旧缓存。后续聚类基于这些特征，并核验辅助网元、角色及覆盖掩码。

## 验收

真实 CSV/SQLite/子进程测试：时区与边界、夏令时、乱序、缺失与真零、counter 首点/下降/缺失/冲突/长间隔、跨文件接续与批次隔离、完整 peer、NetFlow 求和、辅助网元、引用上限、抽样、失败清理与已有输出保护。公开样例全量及第二批前缀运行；不声称聚类或诊断效果。

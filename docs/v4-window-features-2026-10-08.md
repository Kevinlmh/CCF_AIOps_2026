# v4 第二步交付：字段语义与窗口特征

已实现 `aiops_v4/features/`：字段语义登记、显式时间解释、辅助身份、原始序列粒度、窗口统计、临时 SQLite 排序和可复现 CLI。输入继续使用第一步原始观测，不读取旧缓存或任何标签。

## 本次实现

资源按网元、链路按接口、路由按 metric_name 与完整 label 分组；业务流保留原系列与源目标信息，NetFlow 按网元/接口/协议汇总；scrape 和 FRR 作为采集健康与日志上下文。

窗口 epoch 对齐且起点包含、终点不包含。默认 60 秒，可配置；不补造没有观测的窗口，不填零。每个指标同时给出已到达记录中的输入数、缺失数、坏值数、有限值数、真正使用的样本数；同时间重复、冲突另计。指定 expected_step_seconds 才计算基于该假设的时间覆盖。

Gauge 输出原尺度数值统计及窗口内趋势；counter 除原始水平外，单独输出 delta/rate，并在首点、下降、缺失、坏值、同时间冲突和超长间隔处中断差分。路由当前数量（bgp_peer_count、ipv6_route_count）和已有 rate 字段不差分。NetFlow 按分钟数量相加，不将 exported_records 当成独立业务流数量。

每窗口最多 8 个原始引用，包含原始记录 ID、相对路径、记录序号与 CSV 物理行号；counter 可引用上一窗口的前驱。引用不足时明确标明截断。分位数使用最多 128 个蓄水池样本，标明精确或估计状态。

临时数据库按完整序列/时间排序，跨文件乱序也按时间计算；每批独立。输出新目录须在输入目录外，临时构建成功后才发布，summary.json 最后发布。时间无法解析的记录保留在 rejected.jsonl，不删除原始记录。

## 语义与时区核验结论

[官网数据与规则页面](https://challenge.aiops.cn/home/competition/2087843807868489822)（2026-10-08 浏览器读取）确认第二批缺少详细业务流指标和 FRR 日志；允许特征工程，不能将第一批数据或标签直接用于第二批解题。

官网未明确提供全部 CSV 字段单位或原始无时区时间定义。当前语义表没有将任何推断标为官方 verified。CPU/disk_io_util 的百分比、ratio 的比例以及部分速率单位依据字段名与公开样例范围推断，保留 inferred 状态；`carrier_changes` 无充分依据确认累计/速率含义，保持 unknown/unverified，不自动差分。

官方 SDK 对无时区时间使用 UTC。本次还核对公开样例 477 条业务流的 Unix last-batch 时间：474 条与 CSV 时间在 UTC 下相差不超过 10 分钟；按 Asia/Shanghai 解释则 0 条。这支持选择 UTC，不能据此证明所有源的时间语义。因此 CLI 必须显式提供 `--naive-timezone`，保留未获官方明确确认的状态。aware 时间使用自身偏移；歧义/不存在的夏令时时间拒绝并留存。

公开样例有官方允许的 80 个网元及 8 个 probe-vm 辅助身份，后者 candidate=false、network_element_id=null，没有转换成 traffic-vm。

最终审查还发现业务流显式辅助身份被原始读取器覆盖的问题。已修正为仅在显式网元字段缺失时按数据源推断 traffic-vm，并用回归测试确认辅助网元与官方候选的计数器不会串联。

## 实测

配置：60 秒窗口、预期采样步长 60 秒、counter 最大间隔 180 秒、无时区按 UTC。

| 输入 | 读取记录 | 窗口 | 范围 | 拒绝时间解析 |
|---|---:|---:|---|---:|
| sample/case_001 | 604,284 | 71,208 | 全量 | 0 |
| data/stage2/regions | 120,000 | 96,290 | 每文件前 1,000 条 | 0 |

公开样例 NetFlow 从 538,510 条汇总到 5,434 个窗口，求和守恒核对：bytes=6,495,901,607，packets=5,632,000，flow_record_count=544,111。原始 CSV 的和与所有 NetFlow 窗口的和一致。

全部 167,498 个窗口检查了 batch、引用文件存在、行号区间、引用上限和分位数容量。公开样例有 3,522 个引用截断窗口，第二批前缀有 279 个；原始 CSV 未截断。样例 96 个 counter 指标窗口发生过长间隔，第二批前缀 640 个；均未跨间隔计算差分，不等同发生故障。

初次实测公开样例约 30.23 秒、最大常驻内存约 46.5 MiB；第二批前缀约 20.80 秒、约 48.8 MiB。并行单次本机运行，仅用于验证可运行性。第二批 43.2 GB 文件尚未全量构建，第一批完整数据尚未重新下载。

身份修正后重新运行公开样例：29.35 秒、最大常驻内存约 44.3 MiB；最终 windows.jsonl 的 SHA256 与初次运行完全一致。第二批不包含业务流来源，本次身份修正不改变其处理路径。

完整测试 87 项通过，覆盖时间策略、窗口统计、计数器边界、跨文件排序、NetFlow 求和、辅助身份隔离、输出保护和失败清理。审查结论见 [实施记录](v4-window-features-progress.md)。已有 376 个 data/output/outputs/submit.py 文件按大小和 mtime 核对未变。

## 使用与产物

```bash
python -m aiops_v4.features build \
  --root sample/case_001 --batch public-case-001 \
  --naive-timezone UTC --window-seconds 60 \
  --expected-step-seconds 60 --counter-max-gap-seconds 180 \
  --output-dir outputs/v4/my-run/windows --verbose
```

输出：windows.jsonl、summary.json、semantics.json、semantics.csv、rejected.jsonl、report.md。本次实测目录为 outputs/v4/window-features/20261008/；最终公开样例产物位于 public-case-001-full-reviewed/，第二批前缀产物位于 stage2-prefix-1000/。

## 本步结论与下一步

已得到可追溯的窗口特征接口，原值、派生值、缺失和语义假设能够区分。尚未证明簇能分开故障；没有故障输出、LLM 调用或提交操作。

下一步实现**特征矩阵与状态发现**：按角色与视角对齐窗口、保留覆盖掩码、构建稳健参考尺度；原始累计水平不作为默认聚类输入，优先采用 counter rate/delta；枚举状态使用 last/变化次数等明确表达，不能把代码均值当成连续物理量。比较轻量聚类与稳健统计距离，形成状态变化和规则线索的候选事件证据包，不预先把簇号解释为正常/故障。

其后实现共享 LLM 后端、事件确认/定位/分类三个角色、一致性复核、官方导出、消融和复现。较宽窗口、多尺度变化、业务计数器之间的派生比例可根据候选事件实验补充，避免先堆出未经验证的特征库。

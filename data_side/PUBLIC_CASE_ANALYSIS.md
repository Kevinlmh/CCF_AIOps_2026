# 公开三例的可复核归因

以下真值来自项目 `sample/ground_truth.jsonl`。数值由 [`analyze_public_cases.py`](analyze_public_cases.py) 从对应 Case 原始 node CSV 独立重算，完整逐网元变化和观测区间见 [`public_case_evidence.json`](results/public_case_evidence.json)。所谓“同城排名”是该指标在事件窗口相对截取窗口内事件前中位数的变化量排名，属于证据强度比较，不单独证明因果关系。三个 Case 的事件前基线均只有 5 行。

## incident-0013：西安 service-vm-1，CPU 压力

- 真值：`xian-service-vm-1`，`resource.cpu_pressure`，事件时间 2026-07-28 12:39:34–12:52:54 UTC。
- 该网元 `cpu_usage` 事件前中位数 0.55，事件期间 14.50–41.94；`load1` 同时从前值 0.02 升至 0.16–1.84，`load5` 也上升。可用内存比例约 0.926–0.927，变化较小。
- CPU 增幅在 Case 所含西安网元中排第 1；分类为 CPU 压力的直接证据是 CPU 与负载同步上升，不能仅由其他设备的伴随变化归因。

## incident-0017：广州 service-vm-3，内存压力

- 真值：`guangzhou-service-vm-3`，`resource.memory_pressure`，事件时间 2026-07-28 15:15:01–15:24:21 UTC。
- `memory_available_ratio` 从事件前中位数 0.9234 降至 0.8505–0.8552，降幅在同城网元中排第 1。CPU 同时升至 25.34–29.87，说明存在伴随负载；`swap_used_ratio` 基本保持 0.000125。
- 分类依赖可用内存比例的定向下降及官方真值。由于 Swap 未同步变化，不应把“发生交换压力”写成已证实事实。

## incident-0021：武汉 service-vm-2，磁盘 I/O 压力

- 真值：`wuhan-service-vm-2`，`resource.disk_io_pressure`，事件时间 2026-07-28 17:32:17–17:40:54 UTC。
- `disk_io_util` 从事件前中位数 1.78 升至 85.45–100；读速率前值约 3,641，事件期间约 269 万–747 万；写速率前值约 28,035，期间约 271 万–1.023 亿。CPU 也从前值约 1.09 升至 54.62–80.99。
- 磁盘 I/O 增幅在同城网元中排第 1，但 `wuhan-monitor-vm` 等网元也有明显伴随变化，因此仅比较异常大小不足以识别全部传播与因果关系。读写速率物理单位未由正式字典核准。

公开 Case 含 `monitor-vm` 等网元的 node 观测；当前第一批八城特征库缺少八个 `*-monitor-vm` 的观测。样例的可见范围与第一批数据不同，三例的评测分数不能外推为第一批性能。第一批完整 Case 真值尚未提供，当前无从对其逐 Case 给出可靠的根因和类别解释。

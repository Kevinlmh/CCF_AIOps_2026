# v3 服务器 LLM 诊断交接

Mac 默认以 `rules` 完成事件检测和确定性诊断，不存储任何模型权重。服务器只接收每行一个事件的 `evidence.jsonl`；每条包含 `event_id`、冻结的起止时间、带来源和值的 `signals`、8–12 个带进入原因的合法 `candidates`。`evidence_id` 是证据引用键，`role` 区分 `direct`、`symptom`、`auxiliary` 与 `quality`。`auxiliary` 和 `quality` 不应被解释为单独的故障证明。

服务器对每个事件输出一行 JSON，使用以下形状。`evidence_sha256` 必须原样复制对应 `evidence.jsonl` 行中的同名字段，不能自行重新计算：

```json
{"event_id":"v3-e000001","evidence_sha256":"<对应证据包中的 64 位 SHA-256>","root_cause_top5":["xian-service-vm-1","xian-br-1","xian-br-2","xian-cr-1","xian-cr-2"],"fault_category":{"major_category":"resource","sub_category":"cpu_pressure"},"evidence_ids":["node:526:76:node.cpu_usage"],"model":{"repository":"<Hugging Face repository>","revision":"<commit hash>"}}
```

根因必须是同一事件 `candidates` 中互不重复的五个官方网元；分类必须是官方 28 个大类与小类组合之一；引用的证据 ID 必须存在于该事件。服务器不可更改事件起止时间。Mac 导入器会逐事件检查以上条件，并对不合法、重复或缺失的响应回退到 `rules`，写出回退原因。模型元数据仅用于审计，不参与评分。

`evidence_sha256` 覆盖该事件的时间、信号值、候选排序和进入原因。任何一项变化都会使旧响应失效；每次运行的 `run_manifest.json` 还记录输入特征数组、代码、模型响应及输出文件的 SHA-256。

建议在服务器分别测试根因比较和根因条件分类，记录模型 revision、量化方式、prompt 版本、解码参数、合法 JSON 比例、耗时与峰值内存。固定 `evidence.jsonl` 和候选集，再比较 `rules` 与 LLM 的 Top-1、Top-5 及类别变化；只有在相同两批数据范围的官方反馈中才能讨论得分收益。模型文件和响应缓存置于仓库外或被忽略的目录。

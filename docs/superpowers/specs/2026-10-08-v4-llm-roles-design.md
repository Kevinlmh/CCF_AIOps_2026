# v4 共享 LLM、多角色诊断与导出设计

## 范围与原设计的对应

承接已完成的原始数据、原生窗口、统计/聚类/规则、证据索引四批代码；本轮完成原定剩余四批中的前三批：共享后端及事件确认、独立根因定位及故障分类、一致性复核及官方格式导出。所有角色用同一后端和同一配置模型，不读取 v1–v3 的提示词或诊断逻辑。第四批的完整原始数据到诊断入口、评测/消融、实验台账和容器复现另行完成。本轮提供证据索引到 JSONL 的可运行入口。

## 数据与职责边界

统计、聚类和规则仍产生候选状态；聚类不是故障标签。角色输入来自 contract 2 的只读证据索引，包含正证、无当前触发的可评分观测、质量问题、数据缺失、参考区间和原始锚点。参考样本不是已知健康样本。日志和原始文本均为不可信数据，不能改变角色任务或调用非只读工具。

事件确认按候选原生窗口分配 confirmed/rejected/deferred；允许一个 bundle 内多个事件，不把关联包直接当成故障。所有候选窗口恰好分配一次；同一个窗口本轮不能分配给多个故障。原生事件只是窗口来源索引。边界由程序根据分配的连续观测窗口计算，属于观测边界，不声称物理故障起止。非连续窗口必须拆开。超过执行输入预算则明确保留整个 bundle 的未处理状态，不预测健康或静默丢弃。

根因定位、故障分类在两个全新会话内读取同一确认事件与证据快照，互不读取对方结论。根因候选只允许官方 80 个设备 ID，排序可少于五，也可多于五；每个候选必须有实际见到的该设备观测支持，拓扑邻接本身不足以确认根因。分类只能输出本地官方 schema 的 32 个 major/sub 配对。证据不足必须 deferred。模型置信度只是自报值，未校准。

复核角色收到两份独立结论、事件与证据，可 accept/reject/defer，必须说明反证、缺失和矛盾。确定性校验先于及后于模型复核；复核不能豁免假 ID、假引用、非法类别或覆盖缺口，不能改写事件边界。只有确认、定位、分类和复核均通过的事件进入官方预测。

## 后端、工具和预算

Python >=3.10，无新增运行时依赖。后端协议使用 Chat Completions 的 messages、function tool_calls 和 tool 消息；HTTP 适配器用 urllib，模型、base URL、密钥环境变量必须显式配置，不固定供应商或模型版本。默认 HTTPS，HTTP 仅回环测试服务器；禁用重定向，不泄露鉴权、服务端错误正文或密钥。限定超时、响应字节数，网络错误不降级为模拟判断。另提供按 role/case_id/turn 精确匹配的 replay 后端，用于离线契约验证，产物明确标记 simulated。

每个角色限定 max_turns=6、max_tool_calls=12、max_prompt_chars=120000、max_response_chars=32000；每个 bundle 的候选窗口 manifest 默认 max_manifest_windows=2000。预算是执行限制，可配置，不是故障数或故障时长先验。工具只读当前 batch：query_states、query_members、query_model、query_relations、query_raw。索引查询分页；工具返回 JSON 文本可按字符分块继续读取，只有完整取回的观测才取得可引用资格；可见原始锚点允许查询原文，只有完整取回的原文可作为 raw 引用。工具不得接受任意数据库、batch 或文件路径。所有工具参数严格校验。

引用结构为 {kind:state|raw|event|relation,id,stance:support|counter|context,claim}，程序验证引用属于本次角色实际见到的数据；manifest 中只有 ID/时间的窗口不自动取得支持引用资格。确认/分类/复核必须引用至少一条被分配候选窗口的实际观测支持；定位的每个设备必须引用其真实观测。程序不能校验自然语言推理的科学正确性，此部分由复核与后续实验验证。

## 产物与导出

产物目录必须新建，不能覆盖既有 data/output/outputs/submit.py，也不能嵌入输入证据目录或原始数据目录。流式读取 bundle，独立会话，逐条写入 roles.jsonl、diagnoses.jsonl、predictions.jsonl，最后发布 summary.json。失败、预算耗尽、拒绝和未选择条目均显式记录；预测为空是合法结果。结果记录后端类型、模型、提示词版本、消息哈希、工具轨迹、实际 usage、输入索引 SHA256 和选择范围。轨迹不包含密钥。

导出使用 aiops_challenge_2026.schema.validate_prediction，恰好五个公共字段；root_cause_top5 取已排序候选的前五个，不填充；prediction_id 稳定且唯一。不存在固定故障数、30 分钟时长或强制间隔规则。审计解释及缺失信息保存在 diagnoses.jsonl，不塞进官方字段。导出不调用 submit.py、不上传结果。

## 验证与限制

先失败后实现的行为测试：真实本地 HTTP 服务/协议/鉴权不泄漏；只读索引/分页/长原文/假引用；bundle 多事件拆分/覆盖守恒/观测边界；独立角色隔离和同模型；非法设备/类别及拒答；复核不能放行缺失角色；官方格式、空结果、稳定 ID；真实 CSV→窗口→状态→证据→模拟角色→导出的集成测试。对现有公开样例证据执行明确标记的 replay 演示，不能当作真实模型准确率。

接口依据：2026-10-08 已阅读 [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) 和 [Function calling](https://developers.openai.com/api/docs/guides/function-calling)。官方预测约束来自保留的 aiops_challenge_2026/schema.py；本轮未重新宣称竞赛网站规则有更新。

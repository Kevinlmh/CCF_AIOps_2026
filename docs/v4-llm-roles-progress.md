# SDD ledger — plan: docs/superpowers/plans/2026-10-08-v4-llm-roles.md

本轮在现有 v4 执行；起点 984d394，基线 170 passed。用户已要求一次完成三批，不重复等待方案确认；全部任务由主代理实现，最后一次独立审查。

- Task 1 RED 23 failed（模块未实现），GREEN 23 passed；提交 7ebcaa9；全套 193 passed。
- Task 2 RED 19 failed，GREEN 19 passed；提交 6f6d5ae；全套 212 passed。
- Task 3 RED 14 failed，GREEN 14 passed；提交 150a34f；全套 226 passed。
- Task 4 RED 15 failed，GREEN 29 passed（含前一步）；提交 90ac9ea；全套 241 passed。
- Task 5 RED 6 failed（pipeline/CLI 未实现），GREEN 6 passed；补调用统计和 replay 请求体可选保留测试 RED 2 failed→GREEN；公开真实证据四角色 replay 完成，待判断保留且所有哈希和既有文件保护验证通过。

预检接口：后端 Completion→角色执行；候选窗口清单→确认及不可变事件；定位/分类的 result/status/proofs→复核与导出；所有组件→流式入口。无冲突。角色实际引用的事实另保存，复核和导出再次验证。CLI 关闭 replay 请求体内存保留，轨迹流式落盘。HTTP 不重试，未知用量明确标记。

Ruling: 首版用候选窗口的互斥分配组织事件 — 可拆关联包且覆盖可验证 — 若同一窗口同时承载多故障，需要以后扩展重叠分配契约。

Ruling: HTTP 采用兼容 Chat Completions 的适配器，模型由配置指定 — 用户未指定供应商，离线 replay 可验证契约 — 特定供应商不兼容时需另加适配器。

Task 5 提交 57088c6，全套 249 passed。

最终一次独立整批审查：gpt-6.1-sol/high（gpt-6-astra 此前审查调用因配额不可用），只读检查 984d394..57088c6。无 Critical、无 Minor；四项 Important 已全部复现并通过单轮 TDD 修复：

- 工具时间/标识类型：7 failed、8 passed 的回归 RED→15 GREEN；全套 264 passed。测试还验证第二个 bundle 失败时第一份预测/审计保留。
- 置信度整数溢出：确认/定位/分类的契约和 pipeline 共 6 RED→GREEN；全套 270 passed。
- HTTP BadStatusLine/IncompleteRead：真实本地服务器在 adapter 和 pipeline 共 4 RED→GREEN；全套 274 passed。记录固定错误码、屏蔽服务端正文且保留待判断产物。
- 拓扑关系引用 ID：超出包中 8 条上限的 12 条真实 fixture 关系分页测试 1 RED→GREEN；生成 ID 在序列化前，身份不依赖评分元信息，完整交付后才取得引用资格；最终 **275 passed**。

修复后再次公开证据四角色 replay 到新的 `outputs/v4/llm-roles/20261008/public-replay-reviewed-simulated/`，仍为待判断和空预测。全部产物哈希、输入索引和 531 个既有保护文件复核通过。没有再派二次审查，修复由回归及全套测试验证。


审查边界逐项裁决（按审查顺序）：

Final: Ruling: 真实模型准确率、因果推理和置信度校准留给下一批实验 — 本轮只有契约测试和明确模拟，不据此声称效果 — 若跳过实验，可能输出合法但错误的诊断。

Final: Ruling: 真实供应商兼容性、费用和生产延迟暂未实测 — 本地 HTTP 证明协议和失败处理，真实配置由用户指定 — 不兼容或高成本仍需真实小范围试跑发现。

Final: Ruling: 同窗口同时多故障暂沿用互斥分配契约 — 已在首版决策中明确，非静默遗漏 — 可能不能表达共享窗口的并发故障，需要扩展。

Final: Ruling: 第四批原始数据全链路、全量评测、消融和容器复現按原定范围后续完成 — 本轮已完成所要求前三批 — 暂不能一键复现全量真实实验。

Final: Ruling: 既有统计、聚类、规则的科学有效性沿用前批审查，本轮验证证据接口 — 不重做上一批实现 — 若前层假设有偏差会影响模型判断，需消融定位。

Final: Ruling: 本地运行对象、索引和文件系统按可信输入处理，故意伪造或敌意并发替换不纳入本轮边界 — 正常路径仍有只读/哈希/输出保护 — 恶意篡改场景需要额外隔离与完整性验证。

Deferred minors: none.

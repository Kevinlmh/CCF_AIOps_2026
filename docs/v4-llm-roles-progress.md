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

最终整批审查及修复：待最后审查后在此补充。

# v4 整体审查与最后一批交付（2026-10-09）

结论：当前主架构符合最初方案及与师兄讨论的要求。最后一批补齐统一运行、知识卡/状态解释、九种消融、独立评测与错误分析、成本和复现记录。独立审查发现的四项 Important 已用复现测试修复；没有 Critical。代码实现完成与比赛效果验证分开：尚无真实 LLM 诊断准确率、两批全量效果或最优参数的证据。按用户最新要求，Docker 暂不考虑，不纳入验收。

## 与原始设计逐项核对

依据：[最初设计](superpowers/specs/2026-10-08-v4-design.md)、[最后一批设计](superpowers/specs/2026-10-09-v4-experiments-design.md)。不借用 v1–v3 的检测、阈值、根因排序、事件合并或提示词。

| 原始/师兄要求 | 当前实现与核对结果 | 实证限制 |
|---|---|---|
| 先统计原始数据 | data/profile 流式统计七类 CSV：覆盖、缺失/零值/坏值、范围、分位数、时序/重复线索、质量标记；计数精确，分位数为有界样本估计 | prefix 不代表全量分布；重复/基数检查有上限 |
| 原始解析可重新生成 | data/reader 保留原始字段、完整日志、物理行范围、内容 ID；features 重建窗口，不依赖旧特征库 | 旧特征库仍保留；当前主线不读取它 |
| 统计特征与轻量无监督 | 原生粒度 matrix、稳健中位数/MAD、缺失掩码、有界确定性 k-medoids，输出状态变化和候选 | 不强制只有两簇；簇号不是故障类别；参考并非已知健康 |
| 规则与模型互补 | 统计/聚类/领域规则信号单列后组合为候选；知识卡和提示提出需要核验的机制 | 候选需经 LLM 确认；程序规则不直接给最终故障答案 |
| 保留设备/接口/peer/业务粒度 | 分 source/group/entity 建窗，接口与 peer 不被提前压成设备总分；辅助实体不强行映射到合法根因 | 未提供物理拓扑时不推断真实连线或传播方向 |
| 同一个 LLM，多种 system prompt | 配置同一 backend/model，程序调度 confirmation → localization/classification → review；可选 state_explanation | 四角色不是四个不同模型，也没有运行时新增自主 Agent |
| 定位和分类独立 | 两个新会话使用相同证据/事件/知识输入，不交换结论；复核角色才接收两者结果 | 是否比联合角色更准，需要真实消融 |
| 支持、反证、缺失共同参与 | packet 包含 support、未触发观测、质量上下文、模型/参考说明、原始锚点和有来源关系；提示要求解释反证/缺失 | 未触发不等于健康；证据不足明确 deferred |
| 原文可追溯、按需补取 | SQLite 只读 batch 查询；完整 state/event/raw/relation 工具按块交付，未完整读完不能引用 | 原始补取仍需源文件保持原路径/内容；证据包不是整个原始数据集副本 |
| 批次独立 | 第一/第二批分别统计、拟合和诊断；共享通用方法及公开知识，不共用学习缓存 | 两批默认参数只是起点，不代表最优 |
| 事件边界与规范导出 | 每候选窗恰好分配一次、连续区间、由程序重算时间/ID；校验官方 80 网元、32 类合法配对和五字段 | 物理故障起止未知；不硬编码故障数/最长时长/间隔，不盲目跨 bundle 合并 |
| 全链路与实验闭环 | experiments CLI 提供 run/evaluate/ablate/compare；summary/seal 最后发布；调用/usage/耗时/配置/代码/提示/知识/输入输出可核查 | Replay 分数仅验证契约；缺价或 token 覆盖不足时费用为 null |

`aiops_v4` 仅直接复用 `aiops_common.data.observations.city_from_path` 的通用城市解析辅助。官方 schema/evaluator 和公共类别配置继续来自 `aiops_challenge_2026`；推理链没有调用旧诊断实现。data、output/outputs、submit.py、v3 存档按要求保留。本次前后核对543个既有文件的存在/大小/mtime均无变化；已有 public-reviewed evidence.sqlite 的 SHA256 仍为 `0f71c3cf302d22f0fe4d7ab8b7d851a29dd81fded57b6a0e901d08e317b21faf`；v3 存档 tag 仍指向 `5e69701053ff41ce0fc83a47311c280c71cd856e`。没有对100GB级原始数据声称全字节哈希复验。

## 最后一批新增代码

- `experiments/config.py`：严格 JSON 配置，拒绝未知字段、错误类型、非有限值、真值/明文密钥；相对路径按配置文件目录解析。
- `knowledge.py` / `roles.py`：32 类公共知识卡和来源；可选状态解释；联合诊断只有真实 joint trace；程序复核模式明确缺少 LLM 语义复核。
- `run.py` / `records.py`：原始统计到预测的完整运行，阶段耗时、输入范围、部分诊断、模型/usage/费用与源代码指纹。
- `evaluation.py`：完成推理后才读真值，复用官方评测；全 supplied truth 分母，逐条 FN/FP、Dice、时间偏差、根因名次和类别错误。
- `ablation.py`：full、statistics_only、cluster_only、no_rules、joint_roles、program_review、no_knowledge、with_state_explanation、discovery_only，独立重跑与封存比较。
- `__main__.py`、`configs/v4` 与[运行文档](v4-reproduction.md)：本机命令、公共 smoke 和两批全量配置模板。默认必须显式填写真实模型，密钥仅从指定环境变量读取。

## 独立审查与修复证据

一名 fresh reviewer 阅读全部 v4 生产模块、实际使用的共享身份解析、官方 schema/evaluator 及关键边界测试，基于 ffdadc0 独立运行 315 项测试通过。审查使用合成数据，没有把父任务原始数据运行冒充为自己的独立重跑。

| Important | 实际复现 | 已做修复 |
|---|---|---|
| 运行中修改已完成阶段被末尾重封存 | 模型回调改写 states/models.jsonl 后仍成功 | 每个阶段发布即固定文件清单/SHA；下一阶段消费前和成功发布前复验，含 config/knowledge/profile；改写/新增均留失败而非成功 |
| 外部推理文件可复用作真值 | 推理后把同一 replay/topology 路径改成真值再评分 | 保存实际消费文件路径、inode/device、内容 SHA；运行中检查变化；评测拒绝同一路径及硬链接文件身份复用。注入内存 Replay 单列 entries SHA，不假称读取不存在的配置文件 |
| 确认依据和真实角色记录重验不足 | 清空确认 citations，或清空角色 trace/调用数仍导出 | 保存实际 confirmation 关联；从 initial request、各次请求摘要、工具完整分块、最后响应及 totals 重建交付事实；引用证据必须与实际记录相同。所有导出模式重验，缺少实际确认记录不接受 |
| 关系 ID 未进入真实提示 | 注册表有 relation_id，但实际发送 packet 没有 | 所有角色序列化各自 session.packet；实际捕获 confirmation/独立/联合/review 请求中都有相同稳定 relation_id |

`tests/v4/test_final_integrity.py` 的 23 个漏洞复现先全部失败，修复后通过；另验证完整工具多块交付的协议重建与改动拒绝。全套测试更新为 **339 passed**。默认/实验模式均有覆盖。旧运行没有新的阶段封存与初始请求证据，因此不升级它们的可信声明；`integrity_version=2`、`run_schema_version=2` 要求在新目录重新运行才能通过新严格评测/导出。

Deferred minor：可捕获 `KeyboardInterrupt` 尚不写 failure.json，但不会发布成功 summary，已完成阶段仍保留。普通 Exception 会写脱敏 failure.json。SIGKILL/断电不保证记录落盘。

## 本机真实数据验证

下表使用修复后 `ab54f4d` 代码，通过实际 CLI 从原始 CSV 重跑；运行目录和结果如下。UTC 为显式假设，公开参考区间不是已知健康。私有两批只取每文件前 1,000 行，不以此推断全批分布/成绩。原始源文件没有被改写。

| 运行（修复后） | 原始文件/已扫行 | 窗口 | 候选窗/事件/bundle | 调用/预测 | 秒 | 范围 |
|---|---:|---:|---:|---:|---:|---|
| [公开样例摘要](../outputs/v4/experiments/20261009/public-full-reviewed/summary.json) | 56/604,284 | 71,208 | 3,407/1,873/695 | 4/0 | 109.10 | 全原始扫描；选1 bundle；partial |
| [第一批摘要](../outputs/v4/experiments/20261009/stage1-prefix-reviewed/summary.json) | 56/51,481 | 41,095 | 6,347/3,309/2,459 | 1/0 | 75.38 | 每文件前1000行；选1 bundle；partial |
| [第二批摘要](../outputs/v4/experiments/20261009/stage2-prefix-reviewed/summary.json) | 120/120,000 | 96,290 | 10,253/5,624/4,701 | 1/0 | 115.45 | 每文件前1000行；选1 bundle；partial |

公开样例四角色使用已有明确标记的 Replay fixture；复核选择保留判断，所以不会为追求非空结果补预测。第一、二批用空 Replay，只验证候选及“缺少回放 → deferred”的本机链路。没有配置/调用真实付费模型，不能用这些运行声称模型准确率或真实费用。

修复后的[公开契约评测](../outputs/v4/experiments/20261009/public-reviewed-evaluation/report.md) 使用全部3条公开真值，显式 allow_partial；0预测对应 Total=0、TP=0、FP=0、FN=3，标记 simulated_contract_check / valid_for_model_quality=false。这是拒绝强行输出的流程结果，不是模型得分。三次同时在本机运行，耗时不是独占性能基准。

## 官方建议与边界

2026-10-09 核对[官网规则与 FAQ](https://challenge.aiops.cn/home/competition/2087843807868489822)。鼓励多智能体，但多角色本身不直接得分；本方案围绕可追溯根因定位/分类建立角色分工。FAQ 允许可复现特征工程、通用规则、公开知识/SOP/拓扑，要求注明来源和外部依赖；knowledge 卡只提供一般机制与核验建议，不包含测试故障答案。两批分别学习，第二批没有第一批部分源时明确缺失。

技术要求表与 FAQ 对纯规则表述有冲突，保留混合 LLM 默认方案，discovery_only 不视为可提交诊断；不自行保证纯规则提交、外部 API 网络或最终审核环境的合规性。官网提及的时长/间隔不作为程序硬约束。当前缺少官方明确时区、所有单位和计数器重置机制的充分确认，代码保留这些假设/未知，不编造因果链。

## 下一步

编码主线已完成；下一阶段应做实证，不必继续堆 Agent。

1. 配置一个支持工具调用的真实模型，在公开样例少量 bundle 上验证实际协议、引用补取、拒答、耗时与 usage，再做独立公开评测。
2. 分别对第一、第二批运行全量统计与候选发现，核对时间/单位/参考污染、候选规模和内存/磁盘耗时；据结果选择有代表性候选开展模型诊断。
3. 用相同输入范围和同一模型比较九种消融，重点检查规则/统计/聚类、多角色、知识卡各自的作用；根据错误分析决定阈值、参考、缺失策略和是否需要跨 bundle 去重。
4. 最终审核/提交环境另按官方最新要求确认；本次不读取/执行 submit.py，不提交结果。Docker 保持暂停。

完整执行记录及所有自主裁定/代价见[进度记录](v4-experiments-progress.md)。

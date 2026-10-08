# v4 第四阶段：候选关联、证据包与查询

用户要求先回顾设计一致性再继续编码。回顾见docs/v4-design-audit-2026-10-08.md。沿用已批准的v4架构和当前分支，完成LLM之前的证据接口；不调用LLM、不读取标签或旧缓存、不执行submit.py。

## 输入、存储与来源

输入第三阶段完整发布目录：summary/states/matrix/models/events。显式batch，校验数量、ID唯一、state/matrix对应、事件成员候选/连续性/计数、参考摘要链接；继承输入prefix、时间与语义限制。输入摘要记录复现信息，失败不发布。

新evidence.sqlite持久保存每个state/matrix/model/event、完整bundle成员及元数据，SQLite磁盘排序和有索引查询；输入不全部装入内存。一次构建只含一个batch，所有查询必须指定batch；连接只读。输出新目录须在所有输入和原始数据目录之外，不覆盖已有结果，summary最后发布。Python>=3.10标准库。

## 关联和证据包

同网元的严格重叠候选区间形成review_bundle；不同网元/辅助身份不硬合并。接触边界不重叠，隔开的区间不跨空缺合并。传递重叠只表示审阅关联，原事件ID、原group/dimensions和原时间完整保留在数据库；bundle的时间包络不是确认故障时段。

每bundle至多输出32个event_id及32个原始引用，完整成员在SQL中可分页查询。精简支持、无当前触发的观测、质量上下文各最多8条（可配置1..32），保留native grain、score、rule proof、drivers、reference与时间覆盖及原始引用。反证是“该配置下可评分但未触发的观测”，不能宣称正常或自动否定其他接口/peer的线索。上下文默认前后120秒，可配置，时间范围保持半开。

缺失说明：全局source无文件/无行、局部未观测/不可评分、引用截断、prefix范围、时区/单位未verified、拓扑缺少、原始引用未物化。缺失不作为正常证据。

可选拓扑JSON包含非空provenance和edges[{a,b,relation}]，端点必须在官方80网元中、边去重、最多512条。仅关联直接相邻且严格重叠的其他bundle，不推断方向、根因、传播路径，不跨batch。没有可信连线输入时明确unavailable；不会从城市或角色猜边。邻近关联输出最多8条，其全部可通过查询工具获得。

## 原始证据

包内引用以raw record_id锚定。构建可物化这些有限引用：每文件一次RecordReader扫描到最大需要的record_index，逐条验证ID、source_file、index及物理行号后保存完整RawRecord，包含完整日志和五元组。上游扫描范围之外的引用拒绝；文件修改导致ID不一致时拒绝发布。只物化包内引用，不能把该表当作全量原始数据快照。

观测时间重用窗口层显式naive_timezone策略；保留原始时间和数据层暂定timestamp，增加observed_time和时间质量标记，查询按后者，不沿用旧默认UTC来覆盖用户配置。若skip_raw，记录未物化状态；query-raw可从登记的原始文件重新核验引用。所有原始文件只读。

## 查询、产物及验收

CLI `python -m aiops_v4.evidence build/query/query-raw`。build产物：bundles.jsonl、evidence.sqlite、summary.json、report.md。query提供bundle（精简证据包）、members（完整成员分页）、states（source/entity/group/time过滤）、model、relations；query-raw按record_id或显式引用找回完整原始记录。limit1..1000，offset非负，参数化SQL，不静默截断整个日志。不存在引用明确报错，查询不改写数据库。

测试：重叠/接触/空缺/实体隔离；反证与缺失/质量区分；拓扑来源与合法端点；有界包与完整成员分页；跨批拒绝、半开时区查询；raw ID/物理行/多行长日志及显式时区；prefix超范围/修改原文件/缺少输入/重复/计数异常失败不发布；已有输出与文件不改。

实测公开样例及第二批前缀，核对每个原候选恰好进入一个bundle，精简容量、查询分页和原始引用。代码审查一次，重要问题回归RED→GREEN。结果仍是审阅证据包，无故障确认/根因/官方分类准确率。

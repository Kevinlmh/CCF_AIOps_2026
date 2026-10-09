# v4 一体化实验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成最后一批编码，提供可复现混合诊断、独立评测和全部架构审查。
**Architecture:** 复用现有 v4 的每个阶段。实验模式隔离，标签仅在封存推理之后由独立命令读取。
**Tech Stack:** Python >=3.10 / stdlib / pytest / Docker。
**Spec:** docs/superpowers/specs/2026-10-09-v4-experiments-design.md（及最初 v4-design）。

## Global Constraints

- 在现有 v4 执行；不修改现有 data/output/outputs、submit.py、v3 存档。
- 配置无真值、无明文密钥；输入只读，新输出目录；不同 batch 学习隔离。
- 默认四角色共享单模型且定位/分类独立；实验联合角色和无语义复核明示，不伪造 trace。
- Replay 永远 simulated；partial 不报告全量结论；费用未知为 null。
- 每任务先失败测试、后实现，最后全套 `.venv/bin/python -m pytest -q`；不提交、不推送比赛结果。

## Review Focus

1. 原始数据解析/语义/聚类的全项目边界：缺测、未知单位、坏时间、参考污染不得成为健康或因果标签。
2. 跨批次和真值泄漏：路径复用、评测文件与提示/知识卡混淆时，拒绝伪装独立实验。
3. 已完成阶段被篡改或运行中断：不得发布成功/评分；失败日志不能泄露 HTTP 密钥。
4. 联合角色/关闭复核：不能绕过事件支持、实际已读引用或伪造独立模型调用。
5. 模拟、不完整选择、usage 缺失和容器不可运行：报告必须准确区分契约、模型效果、成本和实际复现证据。

### Task 1: 严格配置、公开知识卡、成本与 provenance

**Files:** aiops_v4/experiments/{__init__,config,knowledge,records}.py；tests/v4/test_experiment_config.py。
**Interfaces:** Produces `load_config(path)->dict`, `validate_config(dict,base)->dict`, `knowledge_cards()->dict`, `estimate_cost(summary,pricing)->dict`, `provenance(config)->dict`。
- [ ] 写测试：拒绝真值/密钥/未知字段/bool 数字/非法范围；相对路径解析；32合法配对的公开知识源不含事件答案；部分 token/无报价/模拟费用不作真实账单；提示摘要包含所有角色。
- [ ] 运行 `.venv/bin/python -m pytest tests/v4/test_experiment_config.py -q`，Expected: 新接口不存在导致 FAIL。
- [ ] 实现；范围与既有 stages/Budget 对齐，默认 backend 需明确指定 replay/http/model；不猜模型。
- [ ] 相同命令 Expected: PASS；提交 `feat(v4): add strict experiment configuration and provenance`。
- [ ] task-done 全套 Expected: PASS。

### Task 2: 知识上下文、状态解释、真实角色消融

**Files:** aiops_v4/agents/{prompts,diagnosis,pipeline}.py；aiops_v4/experiments/roles.py；tests/v4/test_experiment_roles.py。
**Interfaces:** Consumes knowledge_cards、现有 EvidenceSession/run_role/validators。Produces `run_diagnosis(...,mode='independent',review_mode='llm',knowledge=True,state_explanation=False)->dict`，与现有 diagnose 相同新目录 artifacts；默认仍调用现有严格导出。
- [ ] 写测试：相同知识传给独立角色而不传对方答案；状态解释实际有独立 trace；joint 三调用及同一模型、不伪造独立 trace；program_review 无 review 调用，仍拒绝无效引用/类别和 deferred；默认 export 拒绝实验项。
- [ ] 运行新测试 Expected: 缺失接口/功能 FAIL。
- [ ] 增加 keyword context 支持和版本化提示；实验队列复用确定性 manifest/确认/验证；genuine joint 验证 localization/classification 各自实际证据。
- [ ] 新测试 PASS；提交 `feat(v4): add grounded role and knowledge ablations`。
- [ ] task-done 全套 Expected: PASS。

### Task 3: 原始数据到预测的一体化运行

**Files:** aiops_v4/experiments/run.py；tests/v4/test_experiment_run.py。
**Interfaces:** Consumes Task 1/2 plus profile_dataset/build_features/discover_states/build_evidence。Produces `run_experiment(config,output_dir,*,backend=None)->dict`，summary.json 和 experiment.json 新目录最后发布。
- [ ] 写测试：实际 CSV 全链路输出1条模拟合法预测；所有输入只读、完整 SHA 记录；prefix/部分 bundle/未决区分；不存在输出/无效 config 预检不调用模型；坏原始文件留 failure 不留成功 summary；discovery_only 空预测但不声称诊断完成。
- [ ] 新测试 Expected: FAIL；实现两次 bounded raw scan，阶段独立固定路径；在运行前完整预检；失败只记异常类型与阶段。
- [ ] 新测试 PASS；提交 `feat(v4): wire raw data to audited experiment runs`。
- [ ] task-done 全套 Expected: PASS。

### Task 4: 隔离评测、误差分析和消融比较

**Files:** aiops_v4/experiments/{evaluation,ablation}.py；tests/v4/test_experiment_evaluation.py。
**Interfaces:** Consumes run_experiment封存summary/config/provenance。Produces `evaluate_run(run_dir,truth,batch,output_dir,*,allow_partial=False)->dict`, `run_ablation(config,out,*,variants=None,replays=None)->dict`, `compare_runs(run_dirs,out)->dict`。
- [ ] 写测试：手工真值验证官方100分及FN/FP/时间/RCA/类别错误；模拟评分不用于效果；拒绝篡改/未封存/batch混用/partial默认评分；标签不改变既有推理摘要；独立变体真实规则/模式/角色开关，重复预处理成本记录；不同 raw scope 不伪装可比。
- [ ] 新测试 FAIL；实现官方 evaluator复用，真值只在摘要校验之后打开；独立新报告；按原始指纹及配置推理范围分组比较。
- [ ] 新测试 PASS；提交 `feat(v4): add sealed evaluation and independent ablations`。
- [ ] task-done 全套 Expected: PASS。

### Task 5: CLI、运行配置和 Docker 复现

**Files:** aiops_v4/experiments/__main__.py；configs/v4/*.json；Dockerfile.v4；.dockerignore；README.md；docs/v4-reproduction.md；tests/v4/test_experiment_cli.py。
**Interfaces:** Consumes全部新接口；CLI run/evaluate/ablate/compare，输出 JSON 摘要，出错无敏感异常原文。
- [ ] 子进程测试配置相对路径、回放、新目录、已存在输出拒绝、评测错 batch/缺参数退出2。
- [ ] 新测试 FAIL；实现 CLI；示例全量 stage1/stage2 同一方法独立 batch，仅 timezone/reference/阈值可配置；无配置密钥，Docker allowlist排除数据和提交脚本。
- [ ] 新测试 PASS；检查 Docker 服务并尝试 build/run，记录实际成功或具体不可用；提交 `feat(v4): add experiment CLI and container reproduction`。
- [ ] task-done 全套 Expected: PASS。

### Task 6: 真实原始输入验证和全部架构审查

**Files:** docs/v4-project-audit-2026-10-09.md；docs/v4-experiments-progress.md；own scratch review package。
**Interfaces:** 所有生产代码/原始设计/官方规则 → 完整审计矩阵。
- [ ] 用真实 public 原始输入运行全链路回放；按资源选择 bounded stage1/stage2 原始统计/链路运行，明示 scope；不把人工回放当模型成绩。
- [ ] 实际 Docker 验证依 engine 可用性；全套测试 Expected: PASS；保护543既有文件stat+关键 evidenceSHA，v3 tag一致。
- [ ] 一名 fresh whole-project reviewer审查整个 aiops_v4 和共享官方 schema/evaluator，严格按 Review Focus；Critical/Important 一次 TDD修复，Minor/Rulings 全量记录。
- [ ] 更新README/审计文档和所有checkbox；提交审计；task-done全套PASS；保存进度后仅清理本计划scratch，保持v4干净。

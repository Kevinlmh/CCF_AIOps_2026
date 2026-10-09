# v4 运行与复现

默认实现：原始统计 → 多视图窗口 → 无监督轻量聚类/统计/规则 → 证据 → 同一 LLM 事件确认 → 独立定位/分类 → LLM 复核 → 官方 JSONL。

## 本机运行

在项目根目录安装 editable package（运行时仅标准库）：

```sh
python -m pip install -e '.[test]'
python -m pytest -q
python -m aiops_v4.experiments run --config configs/v4/public-discovery.json --output-dir outputs/v4/experiments/my-public-discovery
```

`public-discovery.json` 是每文件前1000行的纯线索 smoke，不产生模型判断或可提交预测。stage1.json、stage2.json 为全量模板，共用方法、分批独立学习。运行前设置真实模型 ID 和兼容 Chat Completions 的 base_url；密钥仅通过 AIOPS_LLM_API_KEY 环境变量传入，配置与日志不存明文密钥。不自动读取 .env 或 submit.py。示例 UTC 是显式假设，需通过来源/对齐核验，未获得官方确认。

```sh
python -m aiops_v4.experiments run --config configs/v4/stage1.json --output-dir outputs/v4/experiments/my-stage1
python -m aiops_v4.experiments run --config configs/v4/stage2.json --output-dir outputs/v4/experiments/my-stage2
```

配置相对路径均按配置文件目录解析；运行输出不得存在，不得在 raw_root/data 内。profile.max_rows_per_file 同时限制统计和窗口扫描；取消该字段/设 null 才是全扫描。reference_start/end 不填时使用同批有界离线参考；不是已知健康，首段污染和状态变化需通过实验检查。limit_bundles/bundle_id 是部分诊断，不能称全批结果。根因/类别/引用无法确认则 deferred，不自动补答案。

阶段分别保存 profile（JSON/CSV/Markdown）、features（窗口/拒绝/语义）、states（矩阵/参考/簇/状态/候选）、evidence（完整索引/稀疏原文/证据包）、agents（bundle决策/真实角色 trace/诊断/五字段预测）。根目录保存 config.json、knowledge.json、experiment.json 和最后发布的 summary.json/seal.json。失败只留 failure.json 和已完成阶段，不发布成功摘要。目录使用最终稳定路径，完整复制/搬移后若需要原文补取，应保持 raw 挂载路径；不能假设数据库内绝对路径会自动重写。

## 评测、消融、比较

推理配置/运行命令不接受真值路径。仅公开或合法获得的真值在完成推理后单独评测：

```sh
python -m aiops_v4.experiments evaluate --run-dir outputs/v4/experiments/my-public-run --ground-truth sample/ground_truth.jsonl --batch public-case-001 --output-dir outputs/v4/experiments/my-evaluation
python -m aiops_v4.experiments ablate --config configs/v4/stage2.json --variants full statistics_only cluster_only no_rules joint_roles program_review no_knowledge with_state_explanation discovery_only --output-dir outputs/v4/experiments/my-ablations
python -m aiops_v4.experiments compare --run-dirs outputs/v4/experiments/my-ablations/full outputs/v4/experiments/my-ablations/no_rules --output-dir outputs/v4/experiments/my-comparison
```

partial 默认拒绝评分；确需检查抽样/局部流程时加 `--allow-partial`，报告仍明确 partial，全部 supplied truth 保留在分母。Replay 永远 simulated，分数仅验证流程；不是模型准确率。比较成绩可另给 `--evaluation-dirs EVAL1 EVAL2`，顺序与 run-dirs 一致，检查各 evaluation 封存摘要及同真值 SHA。不同 batch/raw范围/窗口设置/选择不能伪装输入可比；可比输入仍需检查模型和参数差异。摘要是本地完整性校验，不是对恶意重写摘要/封存的加密认证。

消融每个变体独立重跑统计/窗口/状态/证据，不共用可变缓存；耗时包含重复预处理。statistics_only/cluster_only 都关闭规则；no_rules 仍为 hybrid。joint_roles 是一次真实联合调用，不能视为两名独立角色；program_review 保留程序校验，缺少 LLM 语义复核；默认全方案不使用这两种实验模式。discovery_only 不生成可提交结果。回放消融可用 `--replay-map` JSON 指定各变体回放文件，路径按 map 文件目录解析；缺少精确 role/case/turn 对应回放时明确 deferred。

报价选填，例如 `pricing={"currency":"USD","input_per_million":2,"output_per_million":4}`（这些数仅演示参数，不是任何供应商报价）。记录模型/config/代码/全部提示与知识卡 SHA、环境、调用失败、真实 usage 覆盖、阶段耗时；缺报价或任何调用缺输入/输出 token，estimated_cost=null，已知部分另列。Replay token 不是账单。缓存折扣及供应商计费差异需另核验。

## Docker

依据 [Dockerfile 文档](https://docs.docker.com/reference/dockerfile/) 和 [构建上下文文档](https://docs.docker.com/build/concepts/context/) 编写，.dockerignore 使用 allowlist。runtime 只复制代码、公共配置、许可证；test target 另复制合成测试。data、outputs、submit.py、.env、git history、压缩包不进入构建上下文，不上传镜像或数据。

```sh
docker build -f Dockerfile.v4 --target runtime -t aiops-v4:runtime .
docker build -f Dockerfile.v4 --target test -t aiops-v4:test .
docker run --rm aiops-v4:test tests/v4 -q
```

如需不可变重建，先记录基镜像实际 RepoDigest，再用 `--build-arg PYTHON_IMAGE=python@sha256:实际摘要`；浮动 python:3.12-slim 标签只能作为初始解析入口。记录最终镜像 ID、Python 版本、平台与测试结果。HTTP 模型的服务版本/随机性无法通过镜像保证逐字相同，但同模型、配置、提示、证据/trace 均可核查。官方答疑允许模型运行有一定随机性，并没有保证审核环境可访问外部 API。

容器配置使用 `/inputs/raw`、`/inputs/replay.jsonl` 等内部路径；通过额外配置文件只读挂载，不把主机路径直接当容器路径。

```sh
docker run --rm \
  --mount type=bind,src=/absolute/raw,dst=/inputs/raw,readonly \
  --mount type=bind,src=/absolute/container-config.json,dst=/inputs/config.json,readonly \
  --mount type=bind,src=/absolute/new-parent-output,dst=/work \
  -e AIOPS_LLM_API_KEY \
  aiops-v4:runtime run --config /inputs/config.json --output-dir /work/run
```

回放另将 replay文件只读挂载至配置指向的位置，设置 backend.kind=replay；输出父目录可存在，`/work/run` 必须不存在。正式审核前需按组委会网络/API/硬件要求调整运行配置。容器实际验证证据见最新整体审计文档；不能仅凭 Dockerfile 推断 build/run 成功。

## 公共资源来源

[官方规则页](https://challenge.aiops.cn/home/competition/2087843807868489822#1791516042000'state'=6)，核对日期2026-10-09：鼓励多智能体；FAQ Q3-Q7允许可复现特征工程、通用规则、知识库和公开资料，要求注明来源及外部工具依赖。技术要求表与FAQ对纯规则的表述不一致；本方案保留 LLM 混合主线，不由此推定纯规则提交合规。knowledge.py 机理源于公开32类说明，核验建议是通用工程假设，没有任何测试样本答案。[OpenAI function calling 文档](https://developers.openai.com/api/docs/guides/function-calling) 用于兼容工具协议；供应商实际支持仍需运行验证。

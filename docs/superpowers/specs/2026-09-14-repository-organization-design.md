# CCF AIOps 2026 仓库整理与发布设计

## 目标

将 `/Users/likevin/lmh/CCF_AIOps_2026/official` 中的 Git 仓库提升为整个
`/Users/likevin/lmh/CCF_AIOps_2026` 项目仓库，保留官方历史、第一版模型、需求、设计、计划、测试和代表性评测结果，并发布到
`https://github.com/Kevinlmh/CCF_AIOps_2026.git`。

## 最终目录

```text
CCF_AIOps_2026/
├── .git/
├── .gitignore
├── .venv/                         # 仅本地保留
├── README.md
├── LICENSE
├── pyproject.toml
├── docs/
│   ├── requirements/AIOPS_OnePage.md
│   ├── design/
│   │   ├── multisource-hybrid-model-design.md
│   │   └── repository-organization-design.md
│   └── plans/
│       ├── multisource-hybrid-model-v1.md
│       └── repository-organization.md
├── artifacts/public-sample-v1/
│   ├── predictions.jsonl
│   └── evaluator-report.json
├── aiops_challenge_2026/
├── baseline/bian/
├── examples/
├── sample/
├── tests/
└── tools/
```

## 分支语义

- `official-baseline`：指向 Gitee 官方上游的原始 `origin/main` 提交，不包含本地模型修改。
- `hybrid-v1`：包含第一版多源混合模型和整理后的全部项目文档。
- `main`：与 `hybrid-v1` 指向同一稳定提交，作为 GitHub 默认浏览分支。
- 整理完成后删除冗余本地开发分支 `feature/multisource-hybrid-v1`；提交仍由 `hybrid-v1` 和 `main` 完整保留。

## 远端语义

- `upstream`：官方 Gitee 仓库 `https://gitee.com/murina/aiops-challenge-2026.git`，只用于同步官方更新。
- `origin`：用户 GitHub 仓库 `https://github.com/Kevinlmh/CCF_AIOps_2026.git`，用于推送本项目分支。

## 清理边界

删除内容仅限：`build/`、`*.egg-info/`、Python 缓存、重复调试输出，以及父目录下已经确认为空的
`data/`、`notes/`、`outputs/`、`solution/`。保留官方源码、全部公开样例、测试、许可证、最终预测与评测报告。

`.venv/` 不删除，不进入 Git。GitHub 凭据不写入源码、配置、提交或日志。

## 验证

目录移动后重新运行完整单元测试、Python 编译、公开样例推理和官方 evaluator。推送后使用 `git ls-remote origin` 验证三个远程分支均存在，并确认工作区无未提交变更。

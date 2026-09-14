# CCF AIOps 2026 Repository Organization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将整个 CCF AIOps 2026 项目整理为单一 Git 仓库，建立官方与第一版分支并推送 GitHub。

**Architecture:** 保留现有 Git 历史，把仓库元数据和官方源码一起提升到父目录；通过独立分支表达官方快照与混合模型版本，通过 `upstream`/`origin` 区分官方源和个人仓库。

**Tech Stack:** Git、GitHub HTTPS、Python unittest、官方 evaluator。

**Spec:** `docs/superpowers/specs/2026-09-14-repository-organization-design.md`

## Global Constraints

- 不提交 `.venv`、API Key、模型权重、构建产物或重复调试输出。
- `official-baseline` 必须精确指向整理前的官方上游提交。
- 不删除官方样例、评测器、Baseline 源码和第一版模型测试。
- 推送前后都必须验证提交、分支、测试与远端引用。

---

### Task 1: 固化引用与整理文档

**Files:**
- Move: `docs/superpowers/specs/*.md` → `docs/design/*.md`
- Move: `docs/superpowers/plans/*.md` → `docs/plans/*.md`
- Move: `../AIOPS_OnePage.md` → `docs/requirements/AIOPS_OnePage.md`
- Create: `artifacts/public-sample-v1/predictions.jsonl`
- Create: `artifacts/public-sample-v1/evaluator-report.json`

**Interfaces:**
- Consumes: 当前 `feature/multisource-hybrid-v1` 提交和已验证输出。
- Produces: 可独立阅读的项目文档和一组代表性结果。

- [ ] 记录当前 HEAD、官方上游提交和工作区状态。
- [ ] 创建 `official-baseline` 与 `hybrid-v1` 分支引用。
- [ ] 移动需求、设计和计划文件并修正文档链接。
- [ ] 归档最终预测与 evaluator 报告。
- [ ] 使用 `git diff --check` 验证移动结果并提交。

### Task 2: 提升 Git 根目录并清理生成物

**Files:**
- Move: `official/.git` → `.git`
- Move: `official/*` → project root
- Delete: generated build/cache/output directories listed in the spec

**Interfaces:**
- Consumes: 已提交且干净的内部仓库。
- Produces: `/Users/likevin/lmh/CCF_AIOps_2026` 单一仓库根目录。

- [ ] 确认父目录没有与官方源码同名的冲突文件。
- [ ] 逐项移动 Git 元数据与项目内容，不使用宽泛递归删除。
- [ ] 用 `rmdir` 删除已确认的空目录。
- [ ] 删除已归档的可再生成构建物和重复输出。
- [ ] 验证 `git rev-parse --show-toplevel` 返回父目录且工作区干净。

### Task 3: 重新验证整理后的项目

**Files:**
- Verify: all tracked source, tests, docs and artifacts

**Interfaces:**
- Consumes: 新仓库根目录与父级 `.venv`。
- Produces: 整理后仍可运行的模型证据。

- [ ] 运行 `python -m unittest discover -s tests -v`。
- [ ] 运行 `python -m compileall -q baseline aiops_challenge_2026 tools`。
- [ ] 在临时目录运行三个公开样例。
- [ ] 使用官方 evaluator 验证 `Total=98.855556`、`TP=3`、`FP=0`、`FN=0`。
- [ ] 搜索敏感信息和样例答案硬编码。

### Task 4: 配置远端、发布分支并验证

**Files:**
- Modify: Git remote configuration only

**Interfaces:**
- Consumes: Gitee 官方地址和用户提供的 GitHub 地址。
- Produces: GitHub 上的 `official-baseline`、`hybrid-v1` 与 `main`。

- [ ] 把原 `origin` 改名为 `upstream`。
- [ ] 添加 GitHub 为新的 `origin`。
- [ ] 将本地 `main` 快进到 `hybrid-v1` 稳定提交。
- [ ] 推送 `official-baseline`、`hybrid-v1` 和 `main`，不使用强制推送。
- [ ] 用 `git ls-remote origin` 核对三个远程引用。
- [ ] 确认工作区干净并记录最终提交。

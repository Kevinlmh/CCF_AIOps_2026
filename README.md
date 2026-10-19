# CCF AIOps 2026 · v3

多源网络故障检测与根因定位项目。当前阶段已于 **2026-10-19 收尾冻结**，保留源码、测试、公开样例、最终预测和精简结果记录。

v3 以确定性检测生成事件，结合多观察者 NetFlow 证据、LLM 四角色诊断及事件发现，输出故障时间、根因 Top-5 和官方故障类别。规则路径无需 GPU；LLM 为可选服务器流程。

## 冻结结果

| 版本 | 预测数 | 官方分数（项目记录） |
| --- | ---: | ---: |
| D3，最终冻结版 | 634（批1：327 / 批2：89 / 批3：218） | **34.5224** |

最终文件：[artifacts/final-v3/predictions.jsonl](artifacts/final-v3/predictions.jsonl)。版本、源码提交和完整 SHA-256 见 [冻结清单](artifacts/final-v3/manifest.json)。本地根目录 `result.jsonl` 为同一文件的工作副本，不纳入 Git。官方分数来自项目实验记录，仓库未保留官网回执；完整消融对照见 [结果记录](docs/v3-optimization-results-2026-10-18.md)。

## 安装与测试

需要 Python 3.10+。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

测试使用小型构造数据，无需已删除的比赛原始数据或模型。公开样例及真值保留在 `sample/`，公开样例结果见 [artifacts/public-sample-v1/](artifacts/public-sample-v1/)。

## 代码与资料

| 路径 | 用途 |
| --- | --- |
| `aiops_v3/data/`、`build_features.py` | 多源 CSV 接入、分钟聚合、观测掩码、特征库与维度侧车 |
| `aiops_v3/detection.py`、`netflow_*.py` | 事件检测、NetFlow 服务与路径证据 |
| `aiops_v3/diagnosis.py`、`classification.py` | 候选根因、证据包与机制约束 |
| `aiops_v3/roles.py`、`role_runner.py`、`role_import.py` | 确认、定位、分类、复核四角色及逐字段回退 |
| `aiops_v3/discovery*.py`、`rerank.py` | 可选事件发现与根因重排实验 |
| `aiops_v3/run.py`、`merge.py`、`prediction_audit.py` | 推理、官方格式合并与审计 |
| `data_side/` | 可重新运行的数据审计工具及少量字段参考 |
| `tests/v3/` | 回归测试 |
| `artifacts/` | 最终预测、公开样例结果及少量历史运行摘要 |
| `docs/` | 需求、运行说明、最终实验结论和研究资料 |

重新接入数据及命令示例见 [当前状态与运行说明](docs/v3-current-state.md)，赛题背景见 [需求摘要](docs/requirements/AIOPS_OnePage.md)。v4 源码保存在历史分支，当前 `v3` 分支仅保留其 [架构回顾](docs/v4-architecture-report-2026-10-18.md)。

## 清理后的边界

- 已删除本地与服务器的比赛原始数据、下载包、特征库、重复预测、临时缓存及大体积实验中间文件；服务器 Qwen 权重已删除，项目 vLLM 服务已停止。
- `outputs/` 仅保留研究计划的写作源文件与最终导出，后续运行输出仍由 Git 忽略。旧计划、旧交接说明及多数逐行审计表已移除；历史 Git 提交可查阅旧版已跟踪文档。
- Python 环境和公开样例保留，服务器另存源码及共享依赖缓存保留。清理范围与释放空间见 [清理记录](artifacts/cleanup-20261019.json)。
- 冻结预测和历史摘要可核对格式与哈希。由于原始数据、特征库和完整模型响应已删除，当前归档不包含重演 D3 所需的全部输入；恢复全量实验需重新取得数据、权重并生成证据。

许可证：[Apache-2.0](LICENSE)。

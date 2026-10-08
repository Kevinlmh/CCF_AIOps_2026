# CCF AIOps 2026 · v4

v4 当前是供新方案使用的基础分支，尚未实现新的检测、根因分析或 LLM 流程。

## 保留内容

- `aiops_common/data/`：七类 CSV 的读取、时间与网元标准化、计数器差分、解析统计及流式观测接口。
- `aiops_challenge_2026/`：从 `official-baseline` 恢复的官方数据辅助工具、提交格式校验和评测器；类别配置及数量校验更新为当前的 32 类，官方网元配置包含 80 个网元。
- `sample/`、`examples/`：公开样例数据、标签和预测格式示例。
- `tests/shared/`：通用解析和评测行为的回归检查。
- `data/`、`output/outputs/`、本地 `submit.py`：按要求原样保留，包括已有特征库及结果文件。
- `LICENSE`、项目配置与本地运行环境。

通用代码不包含 v3 的检测阈值、特征张量结构、根因排序、事件合并或 LLM 提示词。

## 安装与验证

```bash
python -m pip install -e '.[test]'
python -m pytest -q tests/shared
python -m aiops_challenge_2026.evaluator \
  --ground-truth sample/ground_truth.jsonl \
  --predictions examples/predictions.jsonl \
  --report /tmp/aiops-public-sample-report.json
```

评测器沿用官方评分实现。只校验提交格式与公开类别，不加入 v3 的“必须五个候选”或“最多 30 分钟”限制。

## 使用通用解析器

```python
from pathlib import Path

from aiops_challenge_2026.config import load_public_config
from aiops_common.data.source import CanonicalObservationStream

config = load_public_config("network_elements")
stream = CanonicalObservationStream(
    Path("sample"),
    aliases={city: city for city in config["cities"]},
    valid_roles=config["device_roles"],
    profile="stage1",  # 第二阶段目录使用 "stage2"
)
for observation in stream:
    # 在新方案中消费观测；文本日志可用 stream.drain_text_events() 读取。
    pass
print(stream.stats.files_by_source)
```

该流只能遍历一次；解析器提供数据读取能力，v4 是否采用这些观测形式由新方案决定。

## v3 存档

v3 分支及标签 `v3-archive-2026-10-08` 保留原版本，归档提交为 `5e69701`。
清理出的代码、实验文档、旧测试和本地材料放在项目旁的目录：

```text
../CCF_AIOps_2026_archives/v3-archive-2026-10-08/
```

归档中的 `manifest.json` 记录移动项；`protected-before.json` 记录保留目录及提交脚本的文件元数据。
新方案未定之前不预先创建算法骨架。

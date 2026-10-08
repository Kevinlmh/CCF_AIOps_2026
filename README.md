# CCF AIOps 2026 · v4

v4 采用“统计特征与轻量聚类发现运行状态，规则补充异常线索，同一个 LLM 通过多个角色完成事件确认、根因定位和故障分类”的混合架构。已实现原始数据基础层、窗口特征、稳健参考尺度、轻量聚类和规则候选事件。

总体方案见 [v4 设计](docs/superpowers/specs/2026-10-08-v4-design.md)，窗口定义见 [窗口特征设计](docs/superpowers/specs/2026-10-08-v4-window-features-design.md)，状态发现见 [第三阶段设计](docs/superpowers/specs/2026-10-08-v4-state-discovery-design.md)。LLM Agent 与官方诊断导出尚未实现。

原始数据层见 [数据基础交付记录](docs/v4-data-foundation-2026-10-08.md)；本步实现、实测结论和下一步任务见 [窗口特征交付记录](docs/v4-window-features-2026-10-08.md)。

最新进展与实验结论见 [状态发现交付记录](docs/v4-state-discovery-2026-10-08.md)。

## 保留内容

- `aiops_common/data/`：七类 CSV 的读取、时间与网元标准化、计数器差分、解析统计及流式观测接口。
- `aiops_v4/data/`：新方案的原始观测层；不默认采用旧解析器的差分、聚合和文本摘要。
- `aiops_challenge_2026/`：从 `official-baseline` 恢复的官方数据辅助工具、提交格式校验和评测器；类别配置及数量校验更新为当前的 32 类，官方网元配置包含 80 个网元。
- `sample/`、`examples/`：公开样例数据、标签和预测格式示例。
- `tests/shared/`：通用解析和评测行为的回归检查。
- `data/`、`output/outputs/`、本地 `submit.py`：按要求原样保留，包括已有特征库及结果文件。
- `LICENSE`、项目配置与本地运行环境。

通用代码不包含 v3 的检测阈值、特征张量结构、根因排序、事件合并或 LLM 提示词。

## 安装与验证

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m aiops_challenge_2026.evaluator \
  --ground-truth sample/ground_truth.jsonl \
  --predictions examples/predictions.jsonl \
  --report /tmp/aiops-public-sample-report.json
```

评测器沿用官方评分实现。只校验提交格式与公开类别，不加入 v3 的“必须五个候选”或“最多 30 分钟”限制。

## 使用 v4 数据层

Python 3.10+，运行只依赖标准库。使用单一批次的原始目录；目录中的所有已识别 CSV 都会读取，不能同时放入同一份数据的多个副本后视为独立观测。

```bash
# 全量扫描一个公开样例，生成 profile.json、report.md 和 fields.csv。
python -m aiops_v4.data profile \
  --root sample/case_001 --batch public-case-001 \
  --report-dir outputs/v4/my-run/public-profile --verbose

# 第二批快速检查：仅取每个文件前 1000 条记录。
python -m aiops_v4.data profile \
  --root data/stage2/regions --batch stage2 \
  --max-rows-per-file 1000 \
  --report-dir outputs/v4/my-run/stage2-prefix --verbose

# 可选：建立新 SQLite 证据快照。先用较小范围验证，按需要全量建立。
python -m aiops_v4.data ingest \
  --root sample/case_001 --batch public-case-001 \
  --max-rows-per-file 100 \
  --database outputs/v4/my-run/evidence.sqlite \
  --report-dir outputs/v4/my-run/evidence-profile

# 只读查询；必须指定批次。时间范围为 [start, end)，查询参数须带时区。
python -m aiops_v4.data query \
  --database outputs/v4/my-run/evidence.sqlite --batch public-case-001 \
  --source node --node-id beida-br-1 --limit 5
```

报告目录和数据库必须是新路径，重复执行会拒绝覆盖。输入目录内不能写入报告或数据库。`query` 输出 JSONL，包含原始字段、完整日志、文件相对路径、记录序号及 CSV 物理行号，可供后续 Agent 查询。

```python
from pathlib import Path
from aiops_v4.data.discovery import discover_sources
from aiops_v4.data.reader import RecordReader

for source_file in discover_sources(Path("sample/case_001")):
    with RecordReader(source_file, batch="public-case-001") as records:
        for record in records:
            # 原始字段在 record.raw；无效值和未知网元保留并打质量标记。
            pass
```

统计边界：

- `full` 的计数和数值矩覆盖扫描值；分位数及 MAD 使用有上限、固定种子的蓄水池样本，并标明是否精确。
- `prefix_sample` 是每个文件前 N 行，不能据此判断整个批次分布；逐文件记录是否读完。
- 重复和乱序检查使用有限跟踪窗口，不删除任何记录；高基数字段统计超限会标明。
- 无时区时间暂按 UTC 解析并标记 `assumed_utc`，保留原始时间；校准前不能直接用于事件时间评分。
- 字段单位及计数器类型尚未验证，本层不做差分、聚合、填零或异常判定。
- routing 的通用 value 列只统计结构与数值有效性；数值分布按 metric_name 分开计算并导出。
- 完整读取器按顺序使用；CSV 的字段长度设置在上下文退出时恢复。内存不随总行数增长，但仍需容纳单条原始记录及有上限的统计样本。
- 清单中的 `parsed_records_sha256` 是已解析内容摘要，不是原文件字节哈希。

遇到扫描错误时，`profile` 输出带错误信息的部分报告并返回退出码 2；`ingest` 不发布部分数据库。

## 构建多视角窗口特征

```bash
python -m aiops_v4.features build \
  --root sample/case_001 --batch public-case-001 \
  --naive-timezone UTC --window-seconds 60 \
  --expected-step-seconds 60 --counter-max-gap-seconds 180 \
  --output-dir outputs/v4/my-run/windows --verbose
```

时区须显式指定，aware 时间采用自身偏移；原始无时区时间会按指定时区重新解析。官方读取器及公开业务流的 Unix 时间对齐支持选择 UTC，但官网没有明确确认所有源的无时区时间语义。

输出 `windows.jsonl`、`summary.json`、`semantics.json/csv`、`rejected.jsonl` 和 `report.md`。临时 SQLite 对乱序数据按序列和时间排序，构建成功后才发布新目录。时间无法解析的原始记录完整保存在 rejected 文件。

窗口粒度：resource 按网元；link 按网元/接口；routing 按网元/指标/完整 label；traffic 业务数据按原始业务系列，NetFlow 按网元/接口/协议；另有 collection 和 log_context 观测上下文。NetFlow 汇总不等同实际业务路径，原始五元组保留在引用指向的 CSV 中。

缺失率针对已到达记录中的适用字段；时间覆盖仅在显式指定预期步长时计算。不生成没有观测的空窗口、不填零。counter 保留原始水平统计，并另算 delta/rate；下降、坏值、冲突或超长间隔不产生差分。快照型路由计数与已有 rate 不差分，NetFlow 分钟数量求和。辅助网元标明不能作为官方候选。

语义表明确标明 inferred/unverified，当前未将字段名推断称为官方已验证单位。`--max-rows-per-file` 可用于开发检查，前缀样本不能用于解释全量分布。窗口分位数用 128 点蓄水池，引用上限 8，截断明确标记；counter 引用可包含上一窗口的前驱记录。

## 运行状态发现与候选事件

```bash
python -m aiops_v4.states discover \
  --windows-dir outputs/v4/my-run/windows --batch public-case-001 \
  --reference-start 2026-07-28T12:35:00Z \
  --reference-end 2026-07-28T12:48:00Z \
  --output-dir outputs/v4/my-run/states --verbose
```

输入是窗口层发布的目录，不读取旧特征库或标签。每个网元/接口/peer/目标序列独立建立参考；未知字段排除，状态码使用类别，counter 使用速率。参考窗口完整包含于显式区间；区间只是实验参考，不代表已知正常。省略区间则使用同批次离线无监督拟合，不用于宣称在线因果效果。

输出 matrix.jsonl、models.jsonl、states.jsonl、events.jsonl、summary.json 和 report.md。每特征最多256个参考样本，k-medoids最多64个参考向量；共同有效特征不足时明确不分配状态。参考簇占比来自有界样本，簇号不对应故障标签。

`--mode statistics|cluster|hybrid` 控制统计/聚类触发，规则默认同时开启；用 `--no-rules` 做独立消融。默认统计阈值6、聚类距离3、稀有簇比例0.1，都是实验参数。采集不可用、协议指标为零、drop/error 非零等是观测线索，质量问题单列。连续触发窗口合并；正常窗口和时间空缺终止事件，候选事件仍需要确认。

## 旧通用解析接口

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

该流只能遍历一次，包含计数器差分、NetFlow 聚合等处理选择。v4 原始层使用上面的新接口；旧特征库可在后续核验字段语义和生成过程后选择性适配。

## 下一步

下一步补充候选事件关联与证据查询，核验候选有效性，再实现共享 LLM 后端的事件确认、根因定位和故障分类角色，最后补充复核、官方导出、诊断评测和复现。

## v3 存档

v3 分支及标签 `v3-archive-2026-10-08` 保留原版本，归档提交为 `5e69701`。
清理出的代码、实验文档、旧测试和本地材料放在项目旁的目录：

```text
../CCF_AIOps_2026_archives/v3-archive-2026-10-08/
```

归档中的 `manifest.json` 记录移动项；`protected-before.json` 记录保留目录及提交脚本的文件元数据。

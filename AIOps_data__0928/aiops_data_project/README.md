# AIOps 数据工程与可解释归因项目

这个目录实现数据的四项工作：

1. 数据基本情况统计与可复现 pipeline；
2. 字段统计与字段语义字典；
3. 多源观测数据源头、粒度和诊断作用梳理；
4. 基于官方已有 Case 的窗口证据、根因 Top-5 和故障分类原因解释。

## 快速运行

从项目根目录执行：

```bash
python3 aiops_data_project/run_pipeline.py
```

默认运行只生成数据 inventory、字段字典、源头映射、图表和报告，不从连续原始数据构造官方 Case。

如果需要额外生成连续原始数据上的探索性异常审计结果，可显式运行：

```bash
python3 aiops_data_project/run_pipeline.py --exploratory-cases
```

该结果只用于检查字段和异常模式，不替代官方 Case 文件。

旧的 `run_pipeline.py` 默认不展开和不读取 netflow，因为它解压后约 96 GB；若只想在已解压 NetFlow 上运行旧 inventory，可运行：

```bash
python3 aiops_data_project/run_pipeline.py --include-netflow
```

## P0 完整数据审计

针对 2026 AIOps 挑战赛数据，完整七源审计使用独立入口，不改写原始 CSV 或压缩包：

```bash
python3 aiops_data_project/run_data_audit.py \
  --root "/Users/yuiii/Desktop/2026 AIOps挑战赛数据集" \
  --output-dir aiops_data_project/outputs/data_audit_latest \
  --workers 4
```

审计覆盖 8 城 × 7 源，输出：

- `quality_by_city_source.csv`：行数、时间覆盖、缺失分钟、解析失败、时间顺序、相邻重复；
- `field_quality.csv`：字段缺失、真实零值、数值范围和非法数值；
- `entity_coverage.csv` / `entity_registry.csv`：实体和序列覆盖；
- `entity_relations.csv` / `service_probe_mapping.csv`：接口、FRR、路由邻居、NetFlow、业务探针关系及其 provenance；
- `metric_semantics.csv`：字段 → 变换 → 证据作用 → 缺失/零值语义；
- `time_gaps.csv` / `counter_audit.csv`：时间间隙和可识别的累计计数器行为；
- `audit_manifest.json` / `audit_detail.json`：口径、输入、限制和汇总结果。

NetFlow 直接从原始压缩包流式读取；北大、上海的 `.aa/.ab` 分卷不落地解压。NetFlow 专用审计只保留 P0 所需的覆盖、缺失/零值、相邻重复、序列间隙、实体和关系状态，再与六源结果合并。

当前已经生成的完整结果见 [AIOps_Data_Audit_Report.md](AIOps_Data_Audit_Report.md) 和 `outputs/data_audit_20260928_full/`。

若需要复现公开 baseline 的候选时间窗检测规则，可单独运行：

```bash
python3 aiops_data_project/run_pipeline.py --official-baseline-only
```

该模式遵循公开 five-sigma detector 的窗口规则：前 20% 全局 baseline、5-sigma 异常阈值、相邻 120 秒合并，并排除 FRR syslog 与 netflow。它只用于核对 baseline 的候选窗口组件，不生成官方 Case，也不直接生成官方根因标签或故障分类。

## 目录

```text
aiops_data_project/
├── pipeline/
│   ├── definitions.py       # 字段、数据源、taxonomy、证据规则
│   ├── inventory.py         # 流式数据盘点与字段字典
│   ├── data_audit.py        # 六源通用质量、关系和语义审计
│   ├── netflow_audit_fast.py# 七源中的 NetFlow P0 流式审计
│   └── merge_audit_outputs.py # 六源与 NetFlow 结果合并
│   ├── cases.py             # Case 检测、候选根因和分类
│   └── report.py            # Markdown 汇报文档生成
├── figures/
│   └── generate_figures.py  # PNG/PDF 学术图表
├── outputs/                 # 运行后生成的统计和 Case 结果
├── run_data_audit.py        # 完整七源审计入口
├── run_pipeline.py
└── AIOps_Data_Academic_Report.md
```

完整解释、字段字典、数据量图、多源证据矩阵以及官方样例 Case 归因说明见 [AIOps_Data_Academic_Report.md](AIOps_Data_Academic_Report.md)。

## 设计原则

- 原始压缩包和城市目录不移动、不覆盖；
- CSV 逐行流式处理，不把多 GB 文件整体读入内存；
- 统一时间为 UTC，保留原始字段和原始缺失值语义；
- 已解压文件的字段统计与未展开源的 schema reference 分开保存，避免把未读取数据误报为实测 inventory；
- 官方 Case 的时间窗口来自官方 Case 文件，不由本项目从原始观测数据重新切分；
- `candidate_cases.*` 只用于连续原始数据的探索性异常审计，不冒充官方 ground truth；
- 输出的根因和分类解释必须回溯到 Case 窗口内的字段证据；
- 所有图表由脚本生成，并同时导出 PDF 和 300 DPI PNG。

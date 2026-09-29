# 数据审计交付包导入与 Push 指南

这份包对应本次数据侧 P0 交付，不包含原始数据集，也不包含 `CCF_AIOps_2026` 中原本已有的未提交修改。

## 包里有什么

完整目录是：

```text
aiops_data_project/
├── run_data_audit.py
├── pipeline/
│   ├── data_audit.py
│   ├── netflow_audit_fast.py
│   └── merge_audit_outputs.py
├── AIOps_Data_Audit_Report.md
├── README.md
├── outputs/
│   ├── data_audit_20260928_full/       # 最终七源结果
│   ├── netflow_audit_fast_20260928/    # NetFlow 独立审计中间结果
│   └── data_audit_20260928/            # 六源审计中间结果
└── IMPORT_AND_PUSH_GUIDE.md
```

最终七源结果已经验证：

- 7 个数据源；
- 56 个城市/数据源输入；
- 521,491,569 行；
- NetFlow 465,667,376 行；
- 8 个城市全部覆盖；
- 12 个最终 CSV/JSON 产物完整存在。

## 一、解压

假设压缩包位于桌面：

```bash
cd ~/Desktop
unzip -q AIOps_data_audit_delivery_20260928.zip
```

解压后应看到：

```text
~/Desktop/AIOps_data_audit_delivery_20260928/aiops_data_project/
```

如果压缩包不是放在桌面，把命令中的路径替换为实际位置即可。

## 二、复制回原项目

原项目目录：

```text
~/Desktop/CCF_AIOps_2026
```

先进入原项目并确认当前状态：

```bash
cd ~/Desktop/CCF_AIOps_2026
git status --short --branch
git diff --stat
```

当前原项目已有大量未提交修改。不要执行 `git reset`、`git checkout --` 或其他清理命令，也不要为了导入本包覆盖已有文件。

如果原项目中还没有 `aiops_data_project/`，直接复制整个目录：

```bash
cp -R ~/Desktop/AIOps_data_audit_delivery_20260928/aiops_data_project ./aiops_data_project
```

如果目录已经存在，先查看差异：

```bash
diff -ru \
  ~/Desktop/CCF_AIOps_2026/aiops_data_project \
  ~/Desktop/AIOps_data_audit_delivery_20260928/aiops_data_project
```

确认后再按需合并，不要盲目覆盖。

## 三、检查本次新增内容

在原项目根目录执行：

```bash
git status --short -- aiops_data_project
git diff --stat -- aiops_data_project
git diff -- aiops_data_project/README.md aiops_data_project/IMPORT_AND_PUSH_GUIDE.md
```

本包不应该修改以下目录：

```text
aiops_v2/
baseline/
tests/
sample/
artifacts/
```

如果这些目录在 `git status` 中出现变化，那是原项目已有的工作区修改，不属于本次数据审计包。不要把它们一并加入本次 commit。

## 四、验证代码和结果

先做语法检查：

```bash
python3 -m py_compile \
  aiops_data_project/run_data_audit.py \
  aiops_data_project/pipeline/data_audit.py \
  aiops_data_project/pipeline/netflow_audit_fast.py \
  aiops_data_project/pipeline/merge_audit_outputs.py
```

再检查最终结果：

```bash
python3 - <<'PY'
import csv
import json
from pathlib import Path

out = Path("aiops_data_project/outputs/data_audit_20260928_full")
manifest = json.loads((out / "audit_manifest.json").read_text())
quality = list(csv.DictReader((out / "quality_by_city_source.csv").open()))

assert manifest["source_count"] == 7
assert manifest["file_count"] == 56
assert manifest["city_count"] == 8
assert manifest["row_count"] == 521491569
assert len(quality) == 56
assert sum(int(row["rows"]) for row in quality) == manifest["row_count"]
assert sum(row["source"] == "netflow_5tuple" for row in quality) == 8

print("audit verification passed")
PY
```

注意：`run_data_audit.py` 是完整重跑入口，会重新扫描原始数据。导入已经生成的结果后，不需要为了验证而重新运行它。

## 五、推荐的 commit 方式

先只查看本次目录中的文件：

```bash
git status --short -- aiops_data_project
```

只暂存本次新增目录：

```bash
git add aiops_data_project
```

确认暂存区没有混入原项目已有修改：

```bash
git diff --cached --name-status
git diff --cached --stat
```

确认内容后再提交：

```bash
git commit -m "data: add seven-source audit and propagation contracts"
```

本次 commit 不应该包含：

- 原始 CSV、原始 tar.gz 或解压后的 NetFlow；
- `aiops_v2/` 的模型代码修改；
- `baseline/`、`tests/`、`sample/`、`artifacts/` 的已有工作区修改；
- 未经确认的 457 条预测标签。

## 六、推荐的 push 方式

更安全的方式是先建分支：

```bash
git switch -c data-audit-20260928
git push -u origin data-audit-20260928
```

如果你确认当前就在允许直接更新的分支，并且已经检查过 commit：

```bash
git push origin HEAD
```

push 前最后确认：

```bash
git status --short --branch
git log -1 --oneline
git show --stat --oneline HEAD
```

## 七、模型侧下一步

这次包只完成数据侧 P0，不会自动修改 v2 模型。模型侧需要先消费：

- `outputs/data_audit_20260928_full/metric_semantics.csv`；
- `outputs/data_audit_20260928_full/entity_relations.csv`；
- `outputs/data_audit_20260928_full/service_probe_mapping.csv`；
- `outputs/data_audit_20260928_full/time_gaps.csv`；
- `outputs/data_audit_20260928_full/field_quality.csv`。

拿到完整的 457 条 predictions、inference log 和 feature store 后，再单独修改模型证据审计和特征投影。不要把数据质量审计结果直接转成正常/故障伪标签。

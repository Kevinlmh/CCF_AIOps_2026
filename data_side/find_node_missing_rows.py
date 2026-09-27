"""List node records with missing numeric fields and their exact field sets."""

from __future__ import annotations

import csv
from pathlib import Path

from baseline.bian.preprocessing.multisource import iter_source_files

from scan_raw_quality import RAW, RESULTS


IDENTITY = {"timestamp", "region", "node", "node_type"}
MISSING = {"", "\\N", "NULL", "null", "None", "none", "NA", "N/A", "nan", "NaN"}


def main() -> None:
    findings = []
    for source, path in iter_source_files(RAW):
        if source != "node":
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            numeric = [name for name in reader.fieldnames or [] if name not in IDENTITY]
            if len(numeric) != 12:
                raise ValueError(f"unexpected node numeric fields in {path}: {numeric}")
            for row in reader:
                absent = [name for name in numeric if (row[name] or "").strip() in MISSING]
                if absent:
                    findings.append({
                        **{key: row[key] for key in ("timestamp", "region", "node", "node_type")},
                        "missing_fields": "|".join(absent),
                    })
    output = RESULTS / "node_partial_missing_rows.csv"
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("timestamp", "region", "node", "node_type", "missing_fields"))
        writer.writeheader()
        writer.writerows(findings)
    print(f"node rows with missing numeric fields: {len(findings)}")


if __name__ == "__main__":
    main()

"""Compare common cells in two masked feature stores without loading all features."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .store import open_store


def _identity(value: str | dict) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))


def compare(old_path: Path, new_path: Path) -> dict:
    old, new = open_store(old_path), open_store(new_path)
    if old.start != new.start or old.manifest["minute_count"] != new.manifest["minute_count"]:
        raise ValueError("stores must have the same start time and minute count")
    result = {"old_store": str(old_path), "new_store": str(new_path), "minutes": old.manifest["minute_count"], "modalities": {}}
    for kind in ("node", "edge", "log"):
        old_entities = old.nodes if kind != "edge" else old.edges
        new_entities = new.nodes if kind != "edge" else new.edges
        old_entity_index = {_identity(value): index for index, value in enumerate(old_entities)}
        new_entity_index = {_identity(value): index for index, value in enumerate(new_entities)}
        common_entities = sorted(old_entity_index.keys() & new_entity_index.keys())
        old_indexes = [old_entity_index[key] for key in common_entities]
        new_indexes = [new_entity_index[key] for key in common_entities]
        old_features = old.manifest["features"][kind]
        new_features = new.manifest["features"][kind]
        common_features = sorted(set(old_features) & set(new_features))
        differences = []
        for feature in common_features:
            old_column, new_column = old_features.index(feature), new_features.index(feature)
            old_mask = np.asarray(getattr(old, f"{kind}_mask")[:, old_indexes, old_column], bool)
            new_mask = np.asarray(getattr(new, f"{kind}_mask")[:, new_indexes, new_column], bool)
            mask_mismatches = int(np.count_nonzero(old_mask != new_mask))
            both = old_mask & new_mask
            old_values = np.asarray(getattr(old, f"{kind}_values")[:, old_indexes, old_column], np.float32)
            new_values = np.asarray(getattr(new, f"{kind}_values")[:, new_indexes, new_column], np.float32)
            value_mismatches = int(np.count_nonzero(both & ~np.isclose(old_values, new_values, rtol=1e-5, atol=1e-6)))
            if mask_mismatches or value_mismatches:
                max_abs_diff = float(np.max(np.abs(old_values[both] - new_values[both]))) if np.any(both) else 0.0
                differences.append({"feature": feature, "mask_mismatches": mask_mismatches,
                                    "value_mismatches": value_mismatches, "max_abs_diff": max_abs_diff})
        differences.sort(key=lambda item: (-item["mask_mismatches"] - item["value_mismatches"], item["feature"]))
        result["modalities"][kind] = {
            "old_entity_count": len(old_entities), "new_entity_count": len(new_entities),
            "common_entity_count": len(common_entities),
            "old_feature_count": len(old_features), "new_feature_count": len(new_features),
            "common_feature_count": len(common_features),
            "old_only_features": sorted(set(old_features) - set(new_features)),
            "new_only_features": sorted(set(new_features) - set(old_features)),
            "different_feature_count": len(differences), "differences": differences,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-store", type=Path, required=True)
    parser.add_argument("--new-store", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.old_store, args.new_store)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({kind: {"common_features": item["common_feature_count"],
                             "different_features": item["different_feature_count"]}
                      for kind, item in report["modalities"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()

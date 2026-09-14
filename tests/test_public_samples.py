from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiops_challenge_2026.schema import validate_prediction
from baseline.bian.preprocessing.multisource import iter_source_files
from baseline.bian.run import run


REPO = Path(__file__).parents[1]


def _sources_with_rows(case: Path) -> set[str]:
    result = set()
    for source, path in iter_source_files(case):
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            if next(csv.DictReader(handle), None) is not None:
                result.add(source)
    return result


class PublicSampleTests(unittest.TestCase):
    def test_streaming_case_001_preserves_direct_cpu_root_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "case_001.jsonl"
            run(
                REPO / "sample" / "case_001",
                output,
                ingestion_mode="streaming",
                spatial_split=True,
            )

            record = json.loads(output.read_text().splitlines()[0])

        self.assertEqual(
            record["root_cause_top5"][0]["network_element_id"],
            "xian-service-vm-1",
        )
        self.assertEqual(
            record["fault_category"],
            {"major_category": "resource", "sub_category": "cpu_pressure"},
        )

    def test_every_public_case_runs_without_reading_ground_truth(self):
        original_open = Path.open

        def guarded_open(path: Path, *args, **kwargs):
            if "ground_truth" in path.name.lower():
                raise AssertionError("inference attempted to read ground truth")
            return original_open(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "open", guarded_open):
            for index in range(1, 4):
                case = REPO / "sample" / f"case_{index:03d}"
                output = Path(directory) / f"case_{index:03d}.jsonl"
                inference_log = Path(directory) / f"case_{index:03d}.json"

                run(
                    case,
                    output,
                    prediction_prefix=f"case_{index:03d}_",
                    inference_log=inference_log,
                )

                records = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertGreaterEqual(len(records), 1)
                for record in records:
                    validate_prediction(record)
                report = json.loads(inference_log.read_text())
                covered = {
                    source for source, count in report["source_coverage"].items() if count > 0
                }
                self.assertEqual(covered, _sources_with_rows(case))


if __name__ == "__main__":
    unittest.main()

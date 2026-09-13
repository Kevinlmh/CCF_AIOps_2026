from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from aiops_challenge_2026.schema import validate_prediction
from baseline.bian.run import run


REPO = Path(__file__).parents[1]
FIXTURE = REPO / "tests/fixtures/multisource/case"


class PipelineTests(unittest.TestCase):
    def test_local_hybrid_pipeline_emits_one_legal_prediction_and_seven_source_log(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "prediction.jsonl"
            inference_log = Path(directory) / "inference.json"

            status = run(
                FIXTURE,
                output,
                model="unused",
                use_llm=False,
                prediction_prefix="test_",
                detector="robust",
                decision_backend="local",
                inference_log=inference_log,
            )

            self.assertEqual(status, 0)
            records = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(len(records), 1)
            validate_prediction(records[0])
            self.assertEqual(len(records[0]["root_cause_top5"]), 5)
            report = json.loads(inference_log.read_text())
            self.assertEqual(
                set(report["source_coverage"]),
                {"node", "interface", "routing", "scrape", "traffic", "netflow", "frr"},
            )
            self.assertEqual(report["backend"], "local")
            self.assertEqual(len(report["events"]), 1)


if __name__ == "__main__":
    unittest.main()

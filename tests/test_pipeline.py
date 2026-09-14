from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from aiops_challenge_2026.schema import validate_prediction
from baseline.bian.run import _ordered_parallel_map, _select_llm_candidates, run


REPO = Path(__file__).parents[1]
FIXTURE = REPO / "tests/fixtures/multisource/case"


class PipelineTests(unittest.TestCase):
    def test_parallel_map_preserves_event_order(self):
        result = _ordered_parallel_map(lambda value: value * value, [3, 1, 2], workers=3)
        self.assertEqual(result, [9, 1, 4])

    def test_llm_candidates_are_bounded_but_keep_deterministic_top5(self):
        candidates = tuple(
            {
                "node_id": f"beida-node-{index}",
                "rank": index + 1,
                "score": 1.0 / (index + 1),
                "anomaly_count": 1 if index < 15 else 0,
                "related_anomaly_count": 0,
            }
            for index in range(30)
        )

        selected = _select_llm_candidates(candidates, limit=12)

        self.assertEqual(len(selected), 12)
        self.assertEqual(
            [item["node_id"] for item in selected[:5]],
            [item["node_id"] for item in candidates[:5]],
        )

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

    def test_streaming_ingestion_emits_audited_prediction(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "prediction.jsonl"
            inference_log = Path(directory) / "inference.json"

            status = run(
                FIXTURE,
                output,
                decision_backend="local",
                ingestion_mode="streaming",
                inference_log=inference_log,
            )

            self.assertEqual(status, 0)
            report = json.loads(inference_log.read_text())
            self.assertEqual(report["ingestion_mode"], "streaming")
            self.assertGreater(report["data_audit"]["observations_evaluated"], 0)
            self.assertTrue(report["data_audit"]["rows_conserved"]["node"])
            for record in map(json.loads, output.read_text().splitlines()):
                validate_prediction(record)

    def test_event_checkpoint_can_be_reused_without_reading_csv_files(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "events.json"
            first = Path(directory) / "first.jsonl"
            second = Path(directory) / "second.jsonl"
            inference_log = Path(directory) / "second-inference.json"
            run(FIXTURE, first, event_cache=cache)

            run(
                Path(directory) / "csv-files-do-not-exist",
                second,
                event_cache=cache,
                reuse_event_cache=True,
                inference_log=inference_log,
            )

            self.assertEqual(first.read_text(), second.read_text())
            report = json.loads(inference_log.read_text())
            self.assertGreater(report["source_coverage"]["node"], 0)

    def test_event_checkpoint_rejects_a_different_detector_config_version(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cache = directory / "events.json"
            output = directory / "first.jsonl"
            run(FIXTURE, output, event_cache=cache)
            config = json.loads(
                (REPO / "baseline/bian/config/model_v1.json").read_text()
            )
            config["version"] = "different"
            config_path = directory / "different-config.json"
            config_path.write_text(json.dumps(config))

            with self.assertRaisesRegex(ValueError, "model version"):
                run(
                    FIXTURE,
                    directory / "second.jsonl",
                    config_path=config_path,
                    event_cache=cache,
                    reuse_event_cache=True,
                )


if __name__ == "__main__":
    unittest.main()

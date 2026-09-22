from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiops_challenge_2026.schema import validate_prediction
from baseline.bian.run import (
    _diagnostic_log,
    _localized_event_bounds,
    _ordered_parallel_map,
    _pipeline_fingerprint,
    _select_llm_candidates,
    run,
)
from baseline.bian.anomaly_detector.robust_detector import DetectionDiagnostics
from baseline.bian.preprocessing.observations import AnomalyEvidence, DetectedEvent
from baseline.bian.localization.graph_fusion import RankingResult


REPO = Path(__file__).parents[1]
FIXTURE = REPO / "tests/fixtures/multisource/case"


class PipelineTests(unittest.TestCase):
    def test_localized_bounds_ignore_support_and_unrelated_city_tail(self):
        start = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

        def evidence(minute, node, *, role="trigger"):
            return AnomalyEvidence(
                timestamp=start + timedelta(minutes=minute),
                source="node",
                node_id=node,
                related_node_ids=(),
                metric="node.cpu_usage" if role == "trigger" else "node.load5",
                value=80.0,
                baseline=1.0,
                score=20.0,
                direction="high",
                dimensions=(),
                event_role=role,
            )

        detected = DetectedEvent(
            start=start + timedelta(minutes=4),
            end=start + timedelta(minutes=15),
            peak_time=start + timedelta(minutes=5),
            confidence=0.9,
            evidence=(
                evidence(4, "wuhan-service-vm-2"),
                evidence(5, "wuhan-service-vm-2"),
                evidence(10, "wuhan-service-vm-2", role="support"),
                evidence(12, "wuhan-service-vm-2"),
                evidence(12, "shenyang-service-vm-1"),
            ),
            source_counts={"node": 5},
        )

        bounds = _localized_event_bounds(
            detected,
            "wuhan-service-vm-2",
            {"localized_start_padding_minutes": 1, "localized_end_padding_minutes": 1},
        )

        self.assertEqual(bounds, (start + timedelta(minutes=4), start + timedelta(minutes=6)))

    def test_localized_bounds_never_exceed_official_maximum_duration(self):
        start = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
        points = tuple(
            AnomalyEvidence(
                timestamp=start + timedelta(minutes=minute),
                source="node",
                node_id="wuhan-service-vm-2",
                related_node_ids=(),
                metric="node.cpu_usage",
                value=80.0,
                baseline=1.0,
                score=20.0,
                direction="high",
                dimensions=(),
            )
            for minute in range(30)
        )
        detected = DetectedEvent(
            start=start,
            end=start + timedelta(minutes=30),
            peak_time=start,
            confidence=0.9,
            evidence=points,
            source_counts={"node": 30},
        )

        localized_start, localized_end = _localized_event_bounds(
            detected,
            "wuhan-service-vm-2",
            {
                "localized_start_padding_minutes": 1,
                "localized_end_padding_minutes": 1,
                "max_event_minutes": 30,
            },
        )

        self.assertLessEqual(
            localized_end - localized_start,
            timedelta(minutes=30),
        )

    def test_parallel_map_preserves_event_order(self):
        result = _ordered_parallel_map(lambda value: value * value, [3, 1, 2], workers=3)
        self.assertEqual(result, [9, 1, 4])

    def test_diagnostic_event_coverage_is_clamped_to_observation_grid(self):
        start = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
        minutes = {start: 1.0, start + timedelta(minutes=1): 1.0}
        diagnostics = DetectionDiagnostics(
            minute_energy=minutes,
            trigger_minute_energy=minutes,
            source_energy={},
            evidence_count=1,
            source_coverage={},
            observation_start=start,
            observation_end=start + timedelta(minutes=1),
            open_threshold=1.0,
            keep_threshold=1.0,
        )

        report = _diagnostic_log(
            bundle=None,
            diagnostics=diagnostics,
            detector_name="robust",
            backend_name="local",
            event_logs=[
                {
                    "start_time": "2026-07-28T11:59:00.000Z",
                    "end_time": "2026-07-28T12:02:00.000Z",
                }
            ],
        )

        self.assertEqual(report["energy_summary"]["event_covered_minutes"], 2)
        self.assertEqual(report["energy_summary"]["event_coverage_ratio"], 1.0)

    def test_llm_top1_is_used_for_localized_event_bounds(self):
        deterministic_root = "xian-service-vm-1"
        llm_root = "xian-service-vm-2"
        ranking = RankingResult(
            top5=[{"rank": 1, "network_element_id": deterministic_root}],
            candidates=(),
            by_node={},
        )
        prototype = SimpleNamespace(
            category={"major_category": "resource", "sub_category": "cpu_pressure"},
            confidence=0.9,
            top3=(),
            signals={},
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "baseline.bian.run.rank_candidates", return_value=ranking
        ), patch(
            "baseline.bian.run.classify_event", return_value=prototype
        ), patch(
            "baseline.bian.run.ApiBackend", return_value=object()
        ), patch(
            "baseline.bian.run._llm_event",
            return_value=(
                [{"rank": 1, "network_element_id": llm_root}],
                prototype.category,
            ),
        ), patch(
            "baseline.bian.run._localized_event_bounds"
        ) as localized:
            localized.side_effect = lambda event, root, config: (event.start, event.end)
            run(
                FIXTURE,
                Path(directory) / "prediction.jsonl",
                decision_backend="api",
                api_base="http://127.0.0.1:8000/v1",
                max_events=1,
            )

        self.assertEqual(localized.call_args.args[1], llm_root)

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
            self.assertEqual(report["model_version"], "1.6")
            self.assertEqual(len(report["pipeline_fingerprint"]), 64)
            summary = report["energy_summary"]
            self.assertGreater(summary["total_minutes"], 0)
            for field in (
                "nonzero_minute_ratio",
                "trigger_nonzero_minute_ratio",
                "event_coverage_ratio",
            ):
                self.assertGreaterEqual(summary[field], 0.0)
                self.assertLessEqual(summary[field], 1.0)
            self.assertGreater(summary["event_covered_minutes"], 0)
            self.assertIn("candidate_scope", report["events"][0])
            self.assertIn(
                "excluded_candidate_count",
                report["events"][0]["candidate_scope"],
            )
            self.assertIn("evidence_retention", report)
            self.assertIn("ranking_margin_summary", report)
            self.assertEqual(report["ranking_margin_summary"]["event_count"], 1)

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
            self.assertIn("dropped_evidence_by_role", report["data_audit"])
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

    def test_event_checkpoint_rejects_changed_metric_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cache = directory / "events.json"
            first_semantics = directory / "semantics-a.json"
            second_semantics = directory / "semantics-b.json"
            first_semantics.write_text('{"default":{"kind":"gauge"},"rules":[]}')
            second_semantics.write_text(
                '{"default":{"kind":"gauge","event_role":"support"},"rules":[]}'
            )
            with patch("baseline.bian.run.METRIC_SEMANTICS_PATH", first_semantics):
                run(FIXTURE, directory / "first.jsonl", event_cache=cache)

            with patch("baseline.bian.run.METRIC_SEMANTICS_PATH", second_semantics):
                with self.assertRaisesRegex(ValueError, "pipeline fingerprint"):
                    run(
                        Path("unused"),
                        directory / "second.jsonl",
                        event_cache=cache,
                        reuse_event_cache=True,
                    )

    def test_pipeline_fingerprint_changes_with_detector_source(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            semantics = directory / "semantics.json"
            implementation = directory / "detector.py"
            semantics.write_text('{"default":{},"rules":[]}')
            implementation.write_text("VERSION = 1\n")

            first = _pipeline_fingerprint(
                {"version": "same"},
                semantics,
                implementation_paths=(implementation,),
            )
            implementation.write_text("VERSION = 2\n")
            second = _pipeline_fingerprint(
                {"version": "same"},
                semantics,
                implementation_paths=(implementation,),
            )

        self.assertNotEqual(first, second)

    def test_evidence_checkpoint_can_be_resegmented_without_csv_files(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            evidence_cache = directory / "evidence.json"
            event_cache = directory / "resegmented-events.json"
            first = directory / "first.jsonl"
            second = directory / "second.jsonl"
            third = directory / "third.jsonl"
            run(
                FIXTURE,
                first,
                ingestion_mode="streaming",
                evidence_cache=evidence_cache,
            )

            run(
                directory / "csv-files-do-not-exist",
                second,
                evidence_cache=evidence_cache,
                reuse_evidence_cache=True,
                event_cache=event_cache,
            )

            self.assertEqual(first.read_text(), second.read_text())
            self.assertTrue(event_cache.exists())
            run(
                directory / "csv-files-still-do-not-exist",
                third,
                event_cache=event_cache,
                reuse_event_cache=True,
            )
            self.assertEqual(second.read_text(), third.read_text())

    def test_memory_and_streaming_modes_produce_same_fixture_predictions(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            fixture = directory / "case" / "xian_window" / "processed"
            fixture.mkdir(parents=True)
            (fixture / "routing_metrics.csv").write_text(
                "timestamp,node,metric_name,value\n"
                "2026-08-19T04:05:00Z,service-vm-1,bgp_peer_up,0\n",
                encoding="utf-8",
            )
            memory_output = directory / "memory.jsonl"
            streaming_output = directory / "streaming.jsonl"

            run(directory / "case", memory_output, ingestion_mode="memory")
            run(directory / "case", streaming_output, ingestion_mode="streaming")

            self.assertEqual(memory_output.read_text(), streaming_output.read_text())
            self.assertEqual(len(memory_output.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()

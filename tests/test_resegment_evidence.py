from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from baseline.bian.checkpoint import load_event_checkpoint, save_evidence_checkpoint
from baseline.bian.preprocessing.observations import AnomalyEvidence
from tools.resegment_evidence import resegment


class EvidenceResegmentationTests(unittest.TestCase):
    def test_resegmentation_records_provenance_without_mutating_source(self):
        start = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)
        points = tuple(
            AnomalyEvidence(
                timestamp=start + timedelta(minutes=minute),
                source="node",
                node_id="beida-service-vm-1",
                related_node_ids=(),
                metric="node.cpu_usage",
                value=40.0,
                baseline=1.0,
                score=10.0,
                direction="high",
                dimensions=(),
            )
            for minute in (1, 2, 3)
        )
        config = {
            "version": "test-1.5",
            "detector": {
                "open_threshold": 7.0,
                "keep_threshold": 3.0,
                "min_persistent_trigger_minutes": 2,
                "metric_trigger_tiers": {
                    "node.cpu_usage": [
                        {"min_value": 35.0, "min_persistent_minutes": 3}
                    ]
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "evidence.json"
            output = root / "events.json"
            config_path = root / "config.json"
            save_evidence_checkpoint(
                points,
                source,
                observation_start=start,
                observation_end=start + timedelta(minutes=5),
                source_coverage={"node": 6},
                metadata={"model_version": "1.4"},
            )
            original = source.read_bytes()
            config_path.write_text(json.dumps(config), encoding="utf-8")

            report = resegment(source, output, config_path)
            events, metadata = load_event_checkpoint(output)

            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(len(events), 1)
            self.assertEqual(report["events"], 1)
            self.assertTrue(metadata["audit_resegmentation"])
            self.assertEqual(metadata["source_model_version"], "1.4")


if __name__ == "__main__":
    unittest.main()

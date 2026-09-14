from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from baseline.bian.checkpoint import load_event_checkpoint, save_event_checkpoint
from baseline.bian.preprocessing.observations import AnomalyEvidence, DetectedEvent


class CheckpointTests(unittest.TestCase):
    def test_event_checkpoint_round_trip_preserves_diagnosis_evidence(self):
        start = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)
        evidence = AnomalyEvidence(
            timestamp=start + timedelta(minutes=1),
            source="routing",
            node_id="beida-br-1",
            related_node_ids=("beida-cr-1",),
            metric="routing.bgp_peer_up",
            value=0.0,
            baseline=1.0,
            score=10.0,
            direction="low",
            dimensions=(("peer", "fd00::1"),),
            summary="peer down",
        )
        event = DetectedEvent(
            start=start,
            end=start + timedelta(minutes=3),
            peak_time=evidence.timestamp,
            confidence=0.8,
            evidence=(evidence,),
            source_counts={"routing": 1},
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "events.json"
            save_event_checkpoint([event], target, metadata={"dataset": "stage1"})

            events, metadata = load_event_checkpoint(target)

        self.assertEqual(events, [event])
        self.assertEqual(metadata, {"dataset": "stage1"})

    def test_invalid_checkpoint_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "events.json"
            target.write_text('{"format_version":999,"events":[],"metadata":{}}')

            with self.assertRaisesRegex(ValueError, "checkpoint version"):
                load_event_checkpoint(target)


if __name__ == "__main__":
    unittest.main()

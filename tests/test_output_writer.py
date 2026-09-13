from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from baseline.bian.run import _write_predictions_atomic


VALID = {
    "prediction_id": "pred_000001",
    "start_time": "2026-07-28T12:00:00.000Z",
    "end_time": "2026-07-28T12:05:00.000Z",
    "root_cause_top5": [
        {"rank": 1, "network_element_id": "xian-service-vm-1"},
    ],
    "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"},
}


class OutputWriterTests(unittest.TestCase):
    def test_validated_records_replace_destination_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested/predictions.jsonl"

            _write_predictions_atomic([VALID], output)

            self.assertIn('"prediction_id": "pred_000001"', output.read_text())
            self.assertFalse(any(output.parent.glob(f".{output.name}.*.tmp")))

    def test_invalid_record_does_not_overwrite_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "predictions.jsonl"
            output.write_text("original\n")
            invalid = {**VALID, "fault_category": {"major_category": "unknown", "sub_category": "unknown"}}

            with self.assertRaises(ValueError):
                _write_predictions_atomic([invalid], output)

            self.assertEqual(output.read_text(), "original\n")


if __name__ == "__main__":
    unittest.main()

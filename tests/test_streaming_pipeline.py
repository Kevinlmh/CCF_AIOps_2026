from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from baseline.bian.preprocessing.streaming import detect_events_streaming


REPO = Path(__file__).parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "multisource"
ALIASES = {"xian": "xian"}
ROLES = (
    "br-1", "br-2", "cr-1", "cr-2", "fw", "traffic-vm",
    "service-vm-1", "service-vm-2", "service-vm-3", "monitor-vm",
)


class StreamingPipelineTests(unittest.TestCase):
    def test_all_sources_stream_without_retaining_normal_observations(self):
        config = json.loads(
            (REPO / "baseline" / "bian" / "config" / "model_v1.json").read_text()
        )["detector"]
        with tempfile.TemporaryDirectory() as scratch:
            result = detect_events_streaming(
                FIXTURE,
                ALIASES,
                ROLES,
                config,
                scratch_dir=Path(scratch),
            )

        self.assertEqual(
            set(result.bundle.source_coverage),
            {"node", "interface", "routing", "scrape", "traffic", "netflow", "frr"},
        )
        self.assertEqual(result.bundle.numeric, ())
        self.assertGreater(result.observation_count, 0)
        self.assertGreater(result.series_state_count, 0)
        self.assertTrue(
            all(result.bundle.stats.rows_conserved(source) for source in result.bundle.source_coverage)
        )

    def test_strict_mode_validates_the_requested_city_inventory(self):
        config = json.loads(
            (REPO / "baseline" / "bian" / "config" / "model_v1.json").read_text()
        )["detector"]
        with self.assertRaisesRegex(ValueError, "missing required input"):
            detect_events_streaming(
                FIXTURE,
                ALIASES,
                ROLES,
                config,
                strict_cities=("xian",),
                strict_sources=("node", "missing-source"),
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from baseline.bian.preprocessing.multisource import (
    iter_source_files,
    load_observations,
    validate_source_inventory,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "multisource"
ALIASES = {
    "beida": "beida",
    "shenyang": "shenyang",
    "xian": "xian",
    "chengdu": "chengdu",
    "wuhan": "wuhan",
    "shanghai": "shanghai",
    "nanjing": "nanjing",
    "guangzhou": "guangzhou",
}
VALID_ROLES = (
    "br-1",
    "br-2",
    "cr-1",
    "cr-2",
    "fw",
    "traffic-vm",
    "service-vm-1",
    "service-vm-2",
    "service-vm-3",
    "monitor-vm",
)


def load_fixture():
    return load_observations(FIXTURE_ROOT, ALIASES, VALID_ROLES)


class MultiSourceTests(unittest.TestCase):
    def test_formal_data_directories_are_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "beida_window" / "beida_window_data"
            data.mkdir(parents=True)
            node = data / "node_metrics_20260819_20260902.csv"
            node.write_text("timestamp,region,node,node_type,cpu_usage\n", encoding="utf-8")

            discovered = list(iter_source_files(Path(directory)))

        self.assertEqual(discovered, [("node", node)])

    def test_strict_inventory_rejects_missing_city_source(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "beida_window" / "beida_window_data"
            data.mkdir(parents=True)
            (data / "node_metrics.csv").write_text(
                "timestamp,region,node,node_type,cpu_usage\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "beida.*interface"):
                validate_source_inventory(
                    Path(directory),
                    ALIASES,
                    expected_cities=("beida",),
                    expected_sources=("node", "interface"),
                )

    def test_strict_inventory_rejects_duplicate_city_source(self):
        with tempfile.TemporaryDirectory() as directory:
            for suffix in ("a", "b"):
                data = Path(directory) / f"beida_{suffix}" / f"beida_{suffix}_data"
                data.mkdir(parents=True)
                (data / f"node_metrics_{suffix}.csv").write_text(
                    "timestamp,region,node,node_type,cpu_usage\n", encoding="utf-8"
                )

            with self.assertRaisesRegex(ValueError, "duplicate.*beida.*node"):
                validate_source_inventory(
                    Path(directory),
                    ALIASES,
                    expected_cities=("beida",),
                    expected_sources=("node",),
                )

    def test_missing_required_header_fails_instead_of_silently_dropping_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            processed = Path(directory) / "xian_window" / "processed"
            processed.mkdir(parents=True)
            (processed / "node_metrics.csv").write_text(
                "timestamp,region,node,node_type\n2026-08-19 04:00:00,xian,br-1,br\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "node.*numeric metric"):
                load_observations(Path(directory), ALIASES, VALID_ROLES)

    def test_all_seven_sources_are_discovered_and_counted(self):
        bundle = load_fixture()

        self.assertEqual(
            set(bundle.source_coverage),
            {"node", "interface", "routing", "scrape", "traffic", "netflow", "frr"},
        )
        self.assertEqual(bundle.stats.files_by_source["netflow"], 1)
        self.assertEqual(bundle.stats.rows_by_source["traffic"], 3)
        self.assertEqual(bundle.stats.bad_rows_by_source, {})

    def test_dense_sources_keep_metric_identity_and_dimensions(self):
        bundle = load_fixture()
        observations = list(bundle.numeric)
        metrics = {item.metric for item in observations}

        self.assertIn("node.cpu_usage", metrics)
        self.assertIn("interface.rx_drop_rate", metrics)
        self.assertIn("routing.bgp_peer_up", metrics)
        self.assertIn("scrape.scrape_up", metrics)

        interface = next(item for item in observations if item.metric == "interface.rx_drop_rate")
        self.assertIn(("interface_id", "ens4"), interface.dimensions)
        self.assertIn(("if_role", "uplink"), interface.dimensions)

        routing = next(item for item in observations if item.metric == "routing.bgp_peer_up")
        self.assertIn(("peer", "fd00::1"), routing.dimensions)
        self.assertIn(("afi", "ipv6"), routing.dimensions)

    def test_traffic_counters_become_rates_ratios_and_reset_markers(self):
        bundle = load_fixture()
        traffic = [item for item in bundle.numeric if item.source == "traffic"]

        request_rate = next(
            item
            for item in traffic
            if item.metric == "traffic.web.requests_rate" and item.timestamp.minute == 1
        )
        success_ratio = next(
            item
            for item in traffic
            if item.metric == "traffic.web.success_ratio" and item.timestamp.minute == 1
        )
        error_ratio = next(
            item
            for item in traffic
            if item.metric == "traffic.web.error_ratio" and item.timestamp.minute == 1
        )
        reset = next(
            item
            for item in traffic
            if item.metric == "traffic.web.counter_reset" and item.timestamp.minute == 2
        )

        self.assertEqual(request_rate.value, 30.0)
        self.assertAlmostEqual(success_ratio.value, 25.0 / 30.0)
        self.assertAlmostEqual(error_ratio.value, 5.0 / 30.0)
        self.assertIn(("window_requests", "30.0"), error_ratio.dimensions)
        self.assertEqual(reset.value, 1.0)
        self.assertEqual(request_rate.node_id, "xian-traffic-vm")
        self.assertEqual(
            request_rate.related_node_ids,
            (
                "shanghai-service-vm-1",
                "shanghai-service-vm-2",
                "shanghai-service-vm-3",
            ),
        )

    def test_traffic_ratio_prior_uses_counter_counts_not_per_minute_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            processed = Path(directory) / "xian_window" / "processed"
            processed.mkdir(parents=True)
            (processed / "traffic_flow_metrics.csv").write_text(
                "timestamp_utc,series_key,flow_type,source_region,target_region,"
                "target_domain,protocol,web_flow_requests_total,"
                "web_flow_success_total,web_flow_error_total\n"
                "2026-07-28 12:00:00,flow-a,web,xian,shanghai,"
                "web01.shanghai.aiops.local,http,100,95,5\n"
                "2026-07-28 12:02:00,flow-a,web,xian,shanghai,"
                "web01.shanghai.aiops.local,http,130,120,10\n",
                encoding="utf-8",
            )

            bundle = load_observations(
                Path(directory),
                ALIASES,
                VALID_ROLES,
                detector_config={"traffic_ratio_prior_weight": 30.0},
            )

        traffic = [item for item in bundle.numeric if item.source == "traffic"]
        request_rate = next(
            item for item in traffic if item.metric == "traffic.web.requests_rate"
        )
        success_ratio = next(
            item for item in traffic if item.metric == "traffic.web.success_ratio"
        )
        error_ratio = next(
            item for item in traffic if item.metric == "traffic.web.error_ratio"
        )
        self.assertEqual(request_rate.value, 15.0)
        self.assertAlmostEqual(success_ratio.value, 55.0 / 60.0)
        self.assertAlmostEqual(error_ratio.value, 5.0 / 60.0)

    def test_netflow_rows_are_aggregated_per_minute_observer_and_protocol(self):
        bundle = load_fixture()
        netflow = [
            item
            for item in bundle.numeric
            if item.source == "netflow" and item.timestamp.minute == 0
        ]

        by_metric = {item.metric: item for item in netflow}
        self.assertEqual(by_metric["netflow.bytes"].value, 300.0)
        self.assertEqual(by_metric["netflow.packets"].value, 30.0)
        self.assertEqual(by_metric["netflow.flow_records"].value, 2.0)
        self.assertEqual(by_metric["netflow.unique_sources"].value, 2.0)
        self.assertEqual(by_metric["netflow.unique_destinations"].value, 1.0)
        self.assertEqual(by_metric["netflow.bytes"].node_id, "xian-br-1")
        self.assertIn(("protocol", "6"), by_metric["netflow.bytes"].dimensions)
        self.assertEqual(bundle.stats.warnings, [])

    def test_frr_rows_create_text_events_and_numeric_event_counts(self):
        bundle = load_fixture()
        frr_events = [item for item in bundle.text_events if item.source == "frr"]

        self.assertEqual(len(frr_events), 1)
        self.assertEqual(frr_events[0].event_family, "bgp")
        self.assertEqual(frr_events[0].node_id, "xian-br-1")
        self.assertIn("went Down", frr_events[0].message)

        count = next(item for item in bundle.numeric if item.metric == "frr.event_count")
        self.assertEqual(count.value, 1.0)
        self.assertIn(("event_family", "bgp"), count.dimensions)

    def test_empty_frr_file_is_valid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            processed = Path(directory) / "xian_window" / "processed"
            processed.mkdir(parents=True)
            (processed / "frr_syslog_events.csv").write_text(
                "event_time,hostname,severity,program,message\n",
                encoding="utf-8",
            )

            bundle = load_observations(Path(directory), ALIASES, VALID_ROLES)

        self.assertEqual(bundle.source_coverage, {"frr": 0})
        self.assertEqual(bundle.text_events, ())
        self.assertEqual(bundle.stats.bad_rows_by_source, {})


if __name__ == "__main__":
    unittest.main()

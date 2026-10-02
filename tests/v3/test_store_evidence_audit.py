"""Audits preserve observed gaps, counter semantics, and relation provenance."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import numpy as np

from aiops_v3.data.feature_store import build_feature_store
from aiops_v3.data.observations import NumericObservation
from data_side.store_evidence_audit import audit_feature_store, write_audit


def _store(tmp_path, *, interface_id="ens8"):
    start = datetime(2026, 9, 17, 4, tzinfo=timezone.utc)
    observations = []

    def add(minute, source, node, metric, value, **dimensions):
        observations.append(NumericObservation(
            timestamp=start + timedelta(minutes=minute), source=source,
            node_id=node, related_node_ids=(), metric=metric, value=value,
            dimensions=tuple(dimensions.items()),
        ))

    for minute in (0, 2, 5):
        add(minute, "node", "xian-br-1", "node.cpu_usage", 0)
    for minute in (0, 2):
        add(minute, "interface", "xian-br-1", "interface.rx_drop_rate", 0,
            interface_id=interface_id, if_role="backbone")
        add(minute, "netflow", "xian-br-1", "netflow.packets", 5,
            interface_id=interface_id, protocol="6")
    for minute, value in ((0, 5), (1, 7), (2, 2), (3, 2), (5, 9)):
        add(minute, "routing", "xian-br-1", "routing.ipv6_route_change_total",
            value, prefix="::/0")
    add(1, "routing", "xian-br-1", "routing.bgp_peer_up", 1,
        peer="fd00::2")
    add(4, "routing", "xian-br-1", "routing.bgp_peer_up", 0)
    add(1, "routing", "xian-br-1", "routing.ipv6_default_route_info", 1,
        next_hop="fd00::3")
    add(1, "traffic", "xian-traffic-vm", "traffic.dns.requests_rate", 10,
        flow_type="dns", source_region="xian", target_region="beida",
        target_domain="dns.beida.aiops.local")
    add(2, "traffic", "xian-traffic-vm", "traffic.dns.counter_reset", 1,
        flow_type="dns", source_region="xian", target_region="beida",
        target_domain="dns.beida.aiops.local")
    return build_feature_store(
        observations, {"cities": ["xian", "beida"],
                       "device_roles": ["br-1", "traffic-vm", "service-vm-1"]},
        tmp_path / "store",
    ).path


def test_gap_audit_counts_only_internal_unobserved_minutes(tmp_path):
    report = audit_feature_store(_store(tmp_path))
    cpu = next(row for row in report["series"]
               if row["source"] == "node" and row["metric"] == "node.cpu_usage"
               and row["node_id"] == "xian-br-1")
    assert cpu["observed_minutes"] == 3
    assert cpu["internal_missing_minutes"] == 3
    assert cpu["gap_runs"] == 2
    assert cpu["max_gap_minutes"] == 2
    assert cpu["first_observed_time"] == "2026-09-17T04:00:00Z"
    assert cpu["last_observed_time"] == "2026-09-17T04:05:00Z"
    interface = next(row for row in report["series"]
                     if row["source"] == "interface" and row["metric"] == "interface.rx_drop_rate")
    assert interface["internal_missing_minutes"] == 1
    assert interface["observed_minutes"] == 2
    undimensioned = next(row for row in report["series"]
                         if row["source"] == "routing" and row["metric"] == "routing.bgp_peer_up"
                         and row["dimensions"] == "{}")
    assert undimensioned["observed_minutes"] == 1
    assert undimensioned["first_observed_time"] == "2026-09-17T04:04:00Z"
    assert report["summary"]["gap_semantics"] == "internal_observation_gaps_only"


def test_counter_audit_distinguishes_raw_counters_from_derived_rates(tmp_path):
    report = audit_feature_store(_store(tmp_path))
    routing = next(row for row in report["counters"]
                   if row["metric"] == "routing.ipv6_route_change_total")
    assert routing["counter_semantics"] == "raw_cumulative"
    assert routing["positive_steps"] == 1
    assert routing["unchanged_steps"] == 1
    assert routing["decrease_steps"] == 1
    assert routing["uncompared_gap_pairs"] == 1
    assert routing["first_decrease_time"] == "2026-09-17T04:02:00Z"
    reset = next(row for row in report["counters"]
                 if row["metric"] == "traffic.dns.counter_reset")
    assert reset["counter_semantics"] == "parser_reset_signal"
    assert reset["reset_signal_minutes"] == 1
    assert not any(row["metric"] == "traffic.dns.requests_rate"
                   for row in report["counters"])
    assert report["summary"]["raw_counter_series_count"] == 1
    assert report["summary"]["reset_signal_series_count"] == 1
    assert report["summary"]["counter_audit_rows"] == 2
    assert report["summary"]["counter_decrease_steps"] == 1
    assert report["summary"]["parser_reset_signal_cells"] == 1


def test_relations_keep_observation_basis_without_claiming_physical_topology(tmp_path):
    report = audit_feature_store(_store(tmp_path))
    relations = report["relations"]
    observed = {(row["relation_type"], row["subject_id"], row["object_id"]): row
                for row in relations}
    interface = observed[("owns_observed_interface", "xian-br-1",
                          "interface:xian-br-1:ens8")]
    assert interface["observation_source"] == "interface"
    assert interface["evidence_fields"] == "interface_id"
    assert observed[("observes_netflow_interface", "xian-br-1",
                     "interface:xian-br-1:ens8")]["observation_source"] == "netflow"
    assert observed[("references_route_peer", "xian-br-1", "fd00::2")][
        "observation_source"] == "routing"
    assert observed[("probes_service_domain", "xian-traffic-vm",
                     "dns.beida.aiops.local")]["observation_source"] == "traffic"
    assert observed[("service_domain_targets_city", "dns.beida.aiops.local",
                     "beida")]["observation_source"] == "traffic"
    assert not any(row["relation_type"] in {"physical_link", "service_host"}
                   for row in relations)
    assert report["summary"]["verified_physical_links"] == 0
    assert report["summary"]["relations_by_type"]["owns_observed_interface"] == 1


def test_audit_writes_reviewable_tables(tmp_path):
    report = audit_feature_store(_store(tmp_path))
    output = tmp_path / "audit"
    write_audit(report, output)
    with (output / "entity_relations.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert any(row["relation_type"] == "references_route_next_hop" for row in rows)
    assert b"\r\n" not in (output / "entity_relations.csv").read_bytes()
    assert (output / "summary.json").exists()


def test_interface_relation_uses_same_canonical_id_as_netflow_edge(tmp_path):
    report = audit_feature_store(_store(tmp_path, interface_id="Eth0/1"))
    matching = [row for row in report["relations"]
                if row["relation_type"] in {"owns_observed_interface", "observes_netflow_interface"}]
    assert {row["object_id"] for row in matching} == {"interface:xian-br-1:eth0_1"}


def test_summary_fingerprint_changes_when_store_values_change(tmp_path):
    path = _store(tmp_path)
    before = audit_feature_store(path)["summary"]
    values = np.load(path / "node_values.npy", mmap_mode="r+")
    values[0, 0, 0] = 42
    values.flush()
    after = audit_feature_store(path)["summary"]
    assert before["input_manifest_sha256"] == after["input_manifest_sha256"]
    assert before["input_store_sha256"] != after["input_store_sha256"]

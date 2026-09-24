from types import SimpleNamespace

import numpy as np

from aiops_v2.detection.direct_evidence import score_direct_evidence, select_specific_category


def _store(node_values, names, mask=None):
    values = np.asarray(node_values, dtype=np.float32)
    return SimpleNamespace(
        node_values=values,
        node_mask=np.ones_like(values, dtype=bool) if mask is None else mask,
        log_values=np.zeros((values.shape[0], values.shape[1], 0), dtype=np.float32),
        log_mask=np.zeros((values.shape[0], values.shape[1], 0), dtype=bool),
        features=SimpleNamespace(names=lambda modality: tuple(names) if modality == "node" else ()),
        entities=SimpleNamespace(nodes=tuple(f"city-service-vm-{i+1}" for i in range(values.shape[1]))),
        manifest={"minute_count": values.shape[0]},
    )


def test_direct_evidence_keeps_device_and_metric_identity():
    values = np.ones((90, 2, 3), dtype=np.float32)
    values[:, :, 0] = 2.0
    values[:, :, 1] = 0.92
    values[:, :, 2] = 0.5
    values[35:45, 0, 0] = 45.0
    values[60:70, 1, 1] = 0.82
    result = score_direct_evidence(
        _store(values, ("node.cpu_usage", "node.memory_available_ratio", "node.disk_io_util"))
    )
    assert result.node_probability[40, 0] > 0.8
    assert result.node_probability[40, 1] < 0.5
    assert result.node_probability[65, 1] > 0.8
    assert result.node_probability[65, 0] < 0.5
    assert result.category_scores[40, 0, result.category_names.index("cpu_pressure")] > 0
    assert result.category_scores[65, 1, result.category_names.index("memory_pressure")] > 0


def test_missing_values_and_scrape_failure_do_not_trigger():
    values = np.full((60, 1, 2), (2.0, 1.0), dtype=np.float32)
    values[20:30, 0, 0] = 100.0
    values[20:30, 0, 1] = 0.0
    mask = np.ones_like(values, dtype=bool)
    mask[20:30, 0, 0] = False
    result = score_direct_evidence(
        _store(values, ("node.cpu_usage", "scrape.scrape_up"), mask)
    )
    assert np.max(result.global_probability) < 0.5


def test_sustained_disk_fault_not_absorbed_by_rolling_reference():
    values = np.ones((120, 1, 1), dtype=np.float32)
    values[40:70, 0, 0] = 98.0
    result = score_direct_evidence(_store(values, ("node.disk_io_util",)))
    assert result.node_probability[69, 0] > 0.8
    assert result.node_probability[75, 0] < 0.5


def test_ordinary_one_minute_gauge_spike_needs_support():
    values = np.ones((80, 1, 1), dtype=np.float32)
    values[40, 0, 0] = 30.0
    result = score_direct_evidence(_store(values, ("node.cpu_usage",)))
    assert result.global_probability[40] < 0.8


def test_specific_resource_cause_overrides_generic_cpu_symptom():
    names = ("cpu_pressure", "memory_pressure", "disk_io_pressure")
    assert select_specific_category(np.asarray([12.0, 5.0, 0.0]), names) == "memory_pressure"
    assert select_specific_category(np.asarray([16.0, 0.0, 9.0]), names) == "disk_io_pressure"
    assert select_specific_category(np.asarray([10.0, 0.0, 2.0]), names) == "cpu_pressure"


def test_sustained_routing_state_can_trigger_without_traffic():
    values = np.ones((70, 1, 1), dtype=np.float32)
    values[30:36, 0, 0] = 0.0
    result = score_direct_evidence(_store(values, ("routing.bgp_peer_up",)))
    assert result.global_probability[32] > 0.8
    assert result.global_probability[20] < 0.5


def test_process_count_can_be_primary_trigger():
    values = np.full((80, 1, 1), 100.0, dtype=np.float32)
    values[30:40, 0, 0] = 10000.0
    result = score_direct_evidence(_store(values, ("node.process_count",)))
    assert result.global_probability[35] > 0.8


def test_service_ratio_needs_sufficient_requests_and_persistence():
    node = np.ones((80, 1, 1), dtype=np.float32)
    edge = np.zeros((80, 1, 3), dtype=np.float32)
    edge[:, 0, 1] = 40.0
    edge[20:24, 0, 0] = 0.5
    edge[20:24, 0, 1] = 4.0
    edge[20:24, 0, 2] = 2.0
    edge[45:51, 0, 0] = 0.9
    edge[45:51, 0, 1] = 40.0
    edge[45:51, 0, 2] = 36.0
    # Max ratio comes from a tiny bad series; summed requests mostly succeeded.
    edge[60:66, 0, 0] = 1.0
    edge[60:66, 0, 1] = 1001.0
    edge[60:66, 0, 2] = 1.0
    store = _store(node, ("node.cpu_usage",))
    store.edge_values = edge
    store.edge_mask = np.ones_like(edge, dtype=bool)
    store.entities.edges = (SimpleNamespace(relation="traffic", target="service-group:city:web"),)
    store.features.names = lambda modality: (
        ("node.cpu_usage",) if modality == "node" else
        ("traffic.web.error_ratio", "traffic.web.requests_rate", "traffic.web.error_rate") if modality == "edge" else ()
    )
    result = score_direct_evidence(store)
    assert result.global_probability[22] < 0.8
    assert result.global_probability[48] > 0.8
    assert result.global_probability[63] < 0.8

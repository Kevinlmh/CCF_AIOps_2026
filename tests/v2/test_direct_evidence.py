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


def test_scrape_down_blocks_observed_stale_node_spike():
    values = np.zeros((60, 1, 2), dtype=np.float32)
    values[:, 0, 0] = 2.0
    values[:, 0, 1] = 1.0
    values[20:25, 0, 0] = 80.0
    values[20:25, 0, 1] = 0.0

    result = score_direct_evidence(
        _store(values, ("node.cpu_usage", "scrape.scrape_up"))
    )

    assert np.max(result.node_probability[20:25, 0]) < 0.5
    assert not result.observed[20:25, 0].any()


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


def test_state_dimension_changes_are_scored_as_one_stable_routing_series():
    from types import SimpleNamespace

    values = np.ones((60, 1, 1), dtype=np.float32)
    store = _store(values, ("routing.bgp_peer_up",))
    store.entities.nodes = ("beida-br-1",)
    store.entities.node_index = lambda node_id: 0
    established = SimpleNamespace(
        source="routing",
        node_id="beida-br-1",
        metric="routing.bgp_peer_up",
        dimensions=(("peer", "fd00::1"), ("state", "Established")),
        direction="low",
        aggregation="min",
        time_indices=np.r_[0:25, 31:60],
        values=np.ones(54, dtype=np.float32),
    )
    idle = SimpleNamespace(
        source="routing",
        node_id="beida-br-1",
        metric="routing.bgp_peer_up",
        dimensions=(("peer", "fd00::1"), ("state", "Idle")),
        direction="low",
        aggregation="min",
        time_indices=np.arange(25, 31),
        values=np.zeros(6, dtype=np.float32),
    )
    unscored = SimpleNamespace(
        source="routing",
        node_id="beida-br-1",
        metric="routing.unrecognized_metric",
        dimensions=(("peer", "fd00::2"),),
        direction="both",
        aggregation="max",
        time_indices=np.arange(60),
        values=np.ones(60, dtype=np.float32),
    )
    store.iter_dimension_series = lambda: iter((established, idle, unscored))

    result = score_direct_evidence(store)

    assert result.node_probability[27, 0] > 0.8
    assert result.node_probability[20, 0] < 0.5
    assert result.feature_audit["routing.bgp_peer_up"]["active_cells"] == 6


def test_scrape_down_filters_dimension_series_without_losing_other_minutes():
    values = np.ones((60, 1, 2), dtype=np.float32)
    values[25, 0, 1] = 0.0
    store = _store(values, ("routing.bgp_peer_up", "scrape.scrape_up"))
    store.entities.nodes = ("beida-br-1",)
    store.entities.node_index = lambda node_id: 0
    state = np.ones(60, dtype=np.float32)
    state[25:31] = 0.0
    store.iter_dimension_series = lambda: iter((SimpleNamespace(
        source="routing", node_id="beida-br-1", metric="routing.bgp_peer_up",
        dimensions=(("peer", "fd00::1"),), direction="low", aggregation="min",
        time_indices=np.arange(60), values=state,
    ),))

    result = score_direct_evidence(store)

    assert result.node_probability[25, 0] < 0.5
    assert result.node_probability[27, 0] > 0.8


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


def test_service_latency_needs_observed_requests_and_sufficient_volume():
    node = np.ones((80, 1, 1), dtype=np.float32)
    edge = np.zeros((80, 1, 2), dtype=np.float32)
    edge[:, 0, 0] = 0.5
    edge[:, 0, 1] = 120.0
    edge[20:24, 0, 0] = 3.0
    edge[40:44, 0, 0] = 3.0
    edge[60:64, 0, 0] = 3.0
    edge[40:44, 0, 1] = 1.0
    edge[60:64, 0, 1] = 120.0
    mask = np.ones_like(edge, dtype=bool)
    mask[20:24, 0, 1] = False
    store = _store(node, ("node.cpu_usage",))
    store.edge_values = edge
    store.edge_mask = mask
    store.entities.edges = (SimpleNamespace(relation="traffic", target="service-group:city:web"),)
    store.features.names = lambda modality: (
        ("node.cpu_usage",) if modality == "node" else
        ("traffic.web.latency_p95_seconds", "traffic.web.requests_rate") if modality == "edge" else ()
    )

    result = score_direct_evidence(store)

    assert result.global_probability[22] < 0.8
    assert result.global_probability[42] < 0.8
    assert result.global_probability[62] > 0.8


def test_frr_warning_is_support_not_immediate_event_trigger():
    values = np.ones((40, 1, 1), dtype=np.float32)
    store = _store(values, ("node.cpu_usage",))
    store.log_values = np.zeros((40, 1, 1), dtype=np.float32)
    store.log_values[15, 0, 0] = 1.0
    store.log_mask = np.ones_like(store.log_values, dtype=bool)
    store.features.names = lambda modality: (
        ("node.cpu_usage",) if modality == "node" else
        ("frr.bgp.warning.count",) if modality == "log" else ()
    )

    result = score_direct_evidence(store)

    assert result.global_probability[15] < 0.8


def test_frr_error_is_support_only_even_when_repeated():
    values = np.ones((40, 1, 1), dtype=np.float32)
    store = _store(values, ("node.cpu_usage",))
    store.log_values = np.zeros((40, 1, 1), dtype=np.float32)
    store.log_values[15, 0, 0] = 1.0
    store.log_mask = np.ones_like(store.log_values, dtype=bool)
    store.features.names = lambda modality: (
        ("node.cpu_usage",) if modality == "node" else
        ("frr.bgp.err.count",) if modality == "log" else ()
    )

    result = score_direct_evidence(store)

    assert result.global_probability[15] < 0.8

    store.log_values[16, 0, 0] = 1.0
    supported = score_direct_evidence(store)
    assert supported.global_probability[15] < 0.8
    assert supported.global_probability[16] < 0.8


def test_frr_critical_event_can_open_immediately():
    values = np.ones((40, 1, 1), dtype=np.float32)
    store = _store(values, ("node.cpu_usage",))
    store.log_values = np.zeros((40, 1, 1), dtype=np.float32)
    store.log_values[15, 0, 0] = 1.0
    store.log_mask = np.ones_like(store.log_values, dtype=bool)
    store.features.names = lambda modality: (
        ("node.cpu_usage",) if modality == "node" else
        ("frr.bgp.crit.count",) if modality == "log" else ()
    )

    result = score_direct_evidence(store)

    assert result.global_probability[15] > 0.8


def test_adjacent_different_fault_families_do_not_fake_persistence():
    values = np.ones((80, 1, 2), dtype=np.float32)
    values[:, :, 0] = 2.0
    values[:, :, 1] = 0.95
    values[40, 0, 0] = 45.0
    values[41, 0, 1] = 0.10

    result = score_direct_evidence(
        _store(values, ("node.cpu_usage", "node.memory_available_ratio"))
    )

    assert result.global_probability[40] < 0.8
    assert result.global_probability[41] < 0.8

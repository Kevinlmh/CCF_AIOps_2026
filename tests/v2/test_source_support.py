from types import SimpleNamespace

import numpy as np

from aiops_v2.contracts import EdgeKey
from aiops_v2.detection.source_support import score_frr_support, score_netflow_support


def _store(log_values, log_names, edge_values, edge_names, edge_mask=None, log_mask=None):
    logs = np.asarray(log_values, dtype=np.float32)
    edges = np.asarray(edge_values, dtype=np.float32)
    return SimpleNamespace(
        log_values=logs,
        log_mask=np.ones_like(logs, dtype=bool) if log_mask is None else log_mask,
        edge_values=edges,
        edge_mask=np.ones_like(edges, dtype=bool) if edge_mask is None else edge_mask,
        features=SimpleNamespace(
            names=lambda kind: tuple(log_names) if kind == "log" else tuple(edge_names)
        ),
        entities=SimpleNamespace(
            edges=(EdgeKey("city-br-1", "interface:city-br-1:eth0", "netflow"),)
        ),
    )


def test_frr_error_support_is_bounded_and_missing_or_info_is_zero():
    logs = np.zeros((4, 1, 3), dtype=np.float32)
    logs[1, 0, 0] = 2.0
    logs[2, 0, 1] = 2.0
    logs[3, 0, 2] = 1000.0
    mask = np.ones_like(logs, dtype=bool)
    mask[3, 0, 2] = False
    store = _store(
        logs,
        ("frr.bgp.info.count", "frr.bgp.err.count", "frr.ospf.warning.count"),
        np.zeros((4, 1, 0)), (), log_mask=mask,
    )

    support = score_frr_support(store)

    assert support.shape == (4, 1)
    assert support[1, 0] == 0.0
    assert 0.0 < support[2, 0] <= 8.0
    assert support[3, 0] == 0.0


def test_netflow_volume_only_and_sparse_share_changes_do_not_support_root():
    values = np.zeros((80, 1, 2), dtype=np.float32)
    values[:, 0, 0] = 40.0
    values[:, 0, 1] = 0.2
    values[20:24, 0, 0] = 10000.0  # Volume by itself is not a fault.
    values[40:44, 0, 0] = 2.0
    values[40:44, 0, 1] = 0.9  # Low-flow share is unreliable.
    store = _store(
        np.zeros((80, 1, 0)), (), values,
        ("netflow.flow_records.protocol_6", "netflow.protocol_byte_share.protocol_6"),
    )

    support = score_netflow_support(store)

    assert support.shape == (80, 1)
    assert np.max(support) == 0.0


def test_netflow_sustained_sampled_share_change_supports_source_only():
    values = np.zeros((80, 1, 2), dtype=np.float32)
    values[:, 0, 0] = 40.0
    values[:, 0, 1] = 0.2
    values[30:35, 0, 1] = 0.9
    mask = np.ones_like(values, dtype=bool)
    mask[32, 0, 1] = False
    store = _store(
        np.zeros((80, 1, 0)), (), values,
        ("netflow.flow_records.protocol_6", "netflow.protocol_byte_share.protocol_6"),
        edge_mask=mask,
    )

    support = score_netflow_support(store)

    assert 0.0 < support[30, 0] <= 4.0
    assert support[31, 0] > 0.0
    assert support[32, 0] == 0.0
    assert support[33, 0] > 0.0
    assert support[34, 0] > 0.0
    assert support[25, 0] == 0.0


def test_netflow_single_minute_shifts_in_different_protocols_do_not_form_persistence():
    values = np.zeros((80, 1, 4), dtype=np.float32)
    values[:, 0, :2] = 40.0
    values[:, 0, 2:] = 0.2
    values[30, 0, 2] = 0.9
    values[31, 0, 3] = 0.9
    store = _store(
        np.zeros((80, 1, 0)), (), values,
        (
            "netflow.flow_records.protocol_6", "netflow.flow_records.protocol_17",
            "netflow.protocol_byte_share.protocol_6", "netflow.protocol_byte_share.protocol_17",
        ),
    )

    support = score_netflow_support(store)

    assert support[30, 0] == 0.0
    assert support[31, 0] == 0.0

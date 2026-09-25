"""Bounded auxiliary evidence from observed FRR and NetFlow cells.

These scores support root ranking; NetFlow volume is never a direct trigger.
"""

from __future__ import annotations

import warnings

import numpy as np


def score_frr_support(store) -> np.ndarray:
    """Return severity-aware FRR evidence with shape [minute, node]."""
    values = np.asarray(store.log_values)
    result = np.zeros(values.shape[:2], dtype=np.float32)
    for index, name in enumerate(store.features.names("log")):
        if not name.startswith("frr.") or not name.endswith(".count"):
            continue
        severity = name.rsplit(".", 2)[-2]
        weight = 4.0 if severity in {"err", "crit", "alert", "emerg"} else (
            2.0 if severity == "warning" else 0.0
        )
        if weight == 0.0:
            continue
        data = np.asarray(values[:, :, index], dtype=np.float32)
        observed = np.asarray(store.log_mask[:, :, index], dtype=bool)
        strength = np.where(observed & np.isfinite(data), np.log1p(np.maximum(data, 0.0)) * weight, 0.0)
        np.maximum(result, np.minimum(strength, 8.0), out=result)
    return result


def _neighbors(active: np.ndarray) -> np.ndarray:
    previous = np.zeros_like(active)
    following = np.zeros_like(active)
    previous[1:] = active[:-1]
    following[:-1] = active[1:]
    return active & (previous | following)


def score_netflow_support(store) -> np.ndarray:
    """Return sustained, flow-supported composition shifts with shape [minute, edge]."""
    values = np.asarray(store.edge_values)
    time_count, edge_count, _ = values.shape
    result = np.zeros((time_count, edge_count), dtype=np.float32)
    netflow_edges = np.asarray(
        [edge.relation == "netflow" for edge in store.entities.edges], dtype=bool
    )
    if not netflow_edges.any():
        return result
    names = store.features.names("edge")
    indexes = {name: index for index, name in enumerate(names)}
    for index, name in enumerate(names):
        if not name.startswith((
            "netflow.protocol_byte_share.",
            "netflow.unique_destination_ports.",
            "netflow.unique_destinations.",
        )):
            continue
        protocol = name.rsplit(".", 1)[-1]
        denominator = indexes.get(f"netflow.flow_records.{protocol}")
        if denominator is None:
            continue
        flows = np.asarray(values[:, :, denominator], dtype=np.float32)
        flow_mask = np.asarray(store.edge_mask[:, :, denominator], dtype=bool)
        data = np.asarray(values[:, :, index], dtype=np.float32)
        observed = np.asarray(store.edge_mask[:, :, index], dtype=bool)
        valid = observed & flow_mask & np.isfinite(data) & np.isfinite(flows) & (flows >= 30.0)
        valid &= netflow_edges[None, :]
        if not valid.any():
            continue
        if not name.startswith("netflow.protocol_byte_share."):
            data = np.clip(data / np.maximum(flows, 1.0), 0.0, 1.0)
        clean = np.where(valid, data, np.nan)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            center = np.nanmedian(clean, axis=0)
            mad = np.nanmedian(np.abs(clean - center[None, :]), axis=0)
        scale = np.maximum(np.nan_to_num(1.4826 * mad, nan=0.0), 0.05)
        deviation = np.abs(data - np.nan_to_num(center, nan=0.0)[None, :]) / scale[None, :]
        strength = np.where(valid & (valid.sum(axis=0)[None, :] >= 20), np.clip((deviation - 2.5) / 2.0, 0.0, 4.0), 0.0)
        # Persistence belongs to a feature/edge series, not to a union of
        # unrelated protocols that happened to spike in adjacent minutes.
        strength = np.where(_neighbors(strength > 0), strength, 0.0)
        np.maximum(result, strength, out=result)
    return result

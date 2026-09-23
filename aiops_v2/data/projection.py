"""Project canonical observations into bounded tensor cells."""

from __future__ import annotations

import re

from aiops_v2.contracts import Aggregation, EdgeKey, ProjectedFeature
from baseline.bian.preprocessing.observations import NumericObservation


_SAFE_TOKEN = re.compile(r"[^a-z0-9_-]+")


def _dimensions(observation: NumericObservation) -> dict[str, str]:
    return {key: value for key, value in observation.dimensions}


def _token(value: str, fallback: str = "unknown") -> str:
    clean = _SAFE_TOKEN.sub("_", value.strip().lower()).strip("_")
    return clean or fallback


def _aggregation(observation: NumericObservation) -> Aggregation:
    metric = observation.metric.lower()
    if observation.direction == "low":
        return "min"
    if any(
        token in metric
        for token in (
            "bytes",
            "packets",
            "flow_records",
            "requests_rate",
            "success_rate",
            "error_rate",
            "event_count",
        )
    ):
        return "sum"
    if observation.direction in {"high", "state"}:
        return "max"
    return "max"


def _traffic_feature(observation: NumericObservation) -> ProjectedFeature | None:
    values = _dimensions(observation)
    parts = observation.metric.split(".")
    flow_type = _token(values.get("flow_type") or (parts[1] if len(parts) > 2 else "service"))
    target_region = _token(values.get("target_region", ""), fallback="")
    if not target_region:
        return None
    edge = EdgeKey(
        observation.node_id or "",
        f"service-group:{target_region}:{flow_type}",
        "traffic",
    )
    return ProjectedFeature(
        minute=observation.timestamp.replace(second=0, microsecond=0),
        modality="edge",
        entity=edge,
        feature=observation.metric,
        value=observation.value,
        aggregation=_aggregation(observation),
    )


def _netflow_feature(observation: NumericObservation) -> ProjectedFeature:
    values = _dimensions(observation)
    interface_id = _token(values.get("interface_id", "unknown"))
    protocol = _token(values.get("protocol", "unknown"))
    edge = EdgeKey(
        observation.node_id or "",
        f"interface:{observation.node_id}:{interface_id}",
        "netflow",
    )
    return ProjectedFeature(
        minute=observation.timestamp.replace(second=0, microsecond=0),
        modality="edge",
        entity=edge,
        feature=f"{observation.metric}.protocol_{protocol}",
        value=observation.value,
        aggregation=_aggregation(observation),
    )


def _log_feature(observation: NumericObservation) -> ProjectedFeature:
    values = _dimensions(observation)
    family = _token(values.get("event_family", "other"))
    severity = _token(values.get("severity", "unknown"))
    return ProjectedFeature(
        minute=observation.timestamp.replace(second=0, microsecond=0),
        modality="log",
        entity=observation.node_id or "",
        feature=f"frr.{family}.{severity}.count",
        value=observation.value,
        aggregation="sum",
    )


def project_observation(observation: NumericObservation) -> tuple[ProjectedFeature, ...]:
    """Convert an observation to its model modality without making a decision."""
    if observation.node_id is None:
        return ()
    if observation.source == "traffic":
        feature = _traffic_feature(observation)
        return (feature,) if feature is not None else ()
    if observation.source == "netflow":
        return (_netflow_feature(observation),)
    if observation.source == "frr":
        return (_log_feature(observation),)
    return (
        ProjectedFeature(
            minute=observation.timestamp.replace(second=0, microsecond=0),
            modality="node",
            entity=observation.node_id,
            feature=observation.metric,
            value=observation.value,
            aggregation=_aggregation(observation),
        ),
    )

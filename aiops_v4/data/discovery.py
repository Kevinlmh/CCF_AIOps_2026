"""Discover all recognized source files without assuming a batch layout."""

from pathlib import Path

from .contracts import SourceFile


SOURCE_PREFIXES = {
    "node": "node_metrics", "interface": "interface_metrics",
    "routing": "routing_metrics", "scrape": "scrape_health",
    "traffic": "traffic_flow_metrics", "netflow": "netflow",
    "frr": "frr_syslog_events",
}
TIME_FIELDS = {
    "node": ("timestamp",), "interface": ("timestamp",),
    "routing": ("timestamp",), "scrape": ("timestamp",),
    "traffic": ("timestamp_utc", "prometheus_sample_time_utc"),
    "netflow": ("minute_utc", "first_seen"), "frr": ("event_time", "received_at"),
}
DIMENSION_FIELDS = {
    "node": ("node_type",),
    "interface": ("interface_id", "if_role", "node_type"),
    "routing": ("metric_name", "label", "node_type"),
    "scrape": ("target_id", "exporter_type",),
    "traffic": ("series_key", "flow_type", "source_region", "source_ip",
                "target_region", "target_domain", "protocol"),
    "netflow": ("interface_id", "if_role", "collector_port", "protocol",
                "src_addr", "src_port", "dst_addr", "dst_port", "node_type"),
    "frr": ("program", "severity", "facility", "pid"),
}
IDENTITY_FIELDS = frozenset({
    "id", "timestamp", "timestamp_utc", "prometheus_sample_time_utc", "minute_utc",
    "first_seen", "last_seen", "event_time", "received_at", "inserted_at",
    "received_at_raw", "event_time_raw", "region", "region_code", "node", "node_key",
    "hostname", "node_type", "source_ip", "source_region", "target_region", "target_domain",
    "series_key", "flow_type", "protocol", "interface_id", "if_role", "collector_port",
    "src_addr", "src_port", "dst_addr", "dst_port", "metric_name", "label", "target_id",
    "exporter_type", "scrape_error", "facility", "severity", "severity_code", "program",
    "pid", "message",
})


def numeric_columns(source: str, columns: list[str]) -> list[str]:
    if source == "frr":
        return []
    if source == "routing":
        return ["value"] if "value" in columns else []
    if source == "netflow":
        return [name for name in columns if name in {"packets", "bytes", "flow_record_count"}]
    return [name for name in columns if name not in IDENTITY_FIELDS]


def discover_sources(root: Path) -> list[SourceFile]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"input root is not a directory: {root}")
    result = []
    for path in root.rglob("*.csv"):
        source = next((name for name, prefix in SOURCE_PREFIXES.items()
                       if path.name.lower().startswith(prefix)), None)
        if source is not None:
            if path.is_symlink() or not path.is_file() or root not in path.resolve(strict=True).parents:
                raise ValueError("recognized CSV must be a regular file inside input root: " + path.relative_to(root).as_posix())
            result.append(SourceFile(path, path.relative_to(root).as_posix(), source))
    return sorted(result, key=lambda file: (file.source, file.relative_path))

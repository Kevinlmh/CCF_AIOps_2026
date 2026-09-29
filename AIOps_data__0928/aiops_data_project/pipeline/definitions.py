"""Data contracts used by the AIOps data project.

The definitions are deliberately kept separate from the code that scans files.
This makes the field dictionary auditable: the raw field names remain visible,
while the analytical interpretation can be reviewed or changed independently.
"""

from __future__ import annotations

from typing import Dict, List


SOURCE_DEFINITIONS: List[Dict[str, object]] = [
    {
        "source": "node_metrics",
        "filename_glob": "node_metrics_*.csv",
        "timestamp_field": "timestamp",
        "grain": "one-minute node-level time series",
        "origin": "node exporter / host resource telemetry",
        "entity_fields": ["region", "node", "node_type"],
        "primary_fault_families": ["resource"],
        "baseline_role": "primary anomaly signal",
    },
    {
        "source": "interface_metrics",
        "filename_glob": "interface_metrics_*.csv",
        "timestamp_field": "timestamp",
        "grain": "one-minute interface-level time series",
        "origin": "interface/network exporter telemetry",
        "entity_fields": ["region", "node", "node_type", "interface_id", "if_role"],
        "primary_fault_families": ["link", "firewall", "routing"],
        "baseline_role": "primary anomaly signal",
    },
    {
        "source": "routing_metrics",
        "filename_glob": "routing_metrics_*.csv",
        "timestamp_field": "timestamp",
        "grain": "one-minute node/metric/label time series",
        "origin": "routing exporter; BGP, OSPF6 and IPv6 route telemetry",
        "entity_fields": ["region", "node", "node_type", "metric_name", "label"],
        "primary_fault_families": ["routing"],
        "baseline_role": "primary anomaly signal",
    },
    {
        "source": "scrape_health",
        "filename_glob": "scrape_health_*.csv",
        "timestamp_field": "timestamp",
        "grain": "one-minute exporter-target health time series",
        "origin": "Prometheus scrape health / observability plane",
        "entity_fields": ["region", "target_id", "node", "exporter_type"],
        "primary_fault_families": ["observability"],
        "baseline_role": "quality control and corroboration",
    },
    {
        "source": "traffic_flow_metrics",
        "filename_glob": "traffic_flow_metrics.csv",
        "timestamp_field": "timestamp_utc",
        "grain": "service-flow observation by source, target and flow type",
        "origin": "synthetic service probes / Prometheus flow metrics",
        "entity_fields": ["source_region", "target_region", "target_domain", "flow_type", "protocol"],
        "primary_fault_families": ["service", "link", "firewall"],
        "baseline_role": "primary service-impact signal",
    },
    {
        "source": "frr_syslog_events",
        "filename_glob": "frr_syslog_events_*.csv",
        "timestamp_field": "event_time",
        "grain": "irregular routing-software event",
        "origin": "FRR syslog; bgpd and ospf6d event stream",
        "entity_fields": ["hostname", "region", "program", "severity"],
        "primary_fault_families": ["routing"],
        "baseline_role": "event corroboration",
    },
    {
        "source": "netflow_5tuple",
        "filename_glob": "netflow_5tuple_minute_readable.csv",
        "timestamp_field": "minute_utc",
        "grain": "minute-level five-tuple flow record",
        "origin": "flow collector / NetFlow-like five-tuple records",
        "entity_fields": ["region", "node", "interface_id", "protocol", "src_addr", "dst_addr", "src_port", "dst_port"],
        "primary_fault_families": ["link", "firewall", "service", "routing"],
        "baseline_role": "high-cardinality auxiliary evidence; stream first",
    },
]


FIELD_DEFINITIONS: Dict[str, Dict[str, Dict[str, str]]] = {
    "node_metrics": {
        "timestamp": {"type": "datetime", "unit": "UTC minute", "meaning": "host metric observation time", "role": "join key"},
        "region": {"type": "categorical", "unit": "city alias", "meaning": "observation region", "role": "join key"},
        "node": {"type": "categorical", "unit": "node id", "meaning": "observed network element", "role": "root-cause entity"},
        "node_type": {"type": "categorical", "unit": "role family", "meaning": "device role family", "role": "candidate context"},
        "cpu_usage": {"type": "float", "unit": "ratio", "meaning": "CPU utilization", "role": "resource evidence"},
        "load1": {"type": "float", "unit": "load", "meaning": "one-minute system load", "role": "resource evidence"},
        "load5": {"type": "float", "unit": "load", "meaning": "five-minute system load", "role": "resource evidence"},
        "memory_available_ratio": {"type": "float", "unit": "ratio", "meaning": "available memory ratio", "role": "resource evidence"},
        "swap_used_ratio": {"type": "float", "unit": "ratio", "meaning": "used swap ratio", "role": "resource evidence"},
        "disk_read_rate": {"type": "float", "unit": "bytes/s", "meaning": "disk read rate", "role": "resource evidence"},
        "disk_write_rate": {"type": "float", "unit": "bytes/s", "meaning": "disk write rate", "role": "resource evidence"},
        "disk_io_util": {"type": "float", "unit": "ratio", "meaning": "disk I/O utilization", "role": "resource evidence"},
        "filesystem_used_ratio": {"type": "float", "unit": "ratio", "meaning": "filesystem used ratio", "role": "resource evidence"},
        "inode_used_ratio": {"type": "float", "unit": "ratio", "meaning": "inode used ratio", "role": "resource evidence"},
        "open_fd_ratio": {"type": "float", "unit": "ratio", "meaning": "open file descriptor ratio", "role": "resource evidence"},
        "process_count": {"type": "float", "unit": "count", "meaning": "running process count", "role": "resource evidence"},
    },
    "interface_metrics": {
        "timestamp": {"type": "datetime", "unit": "UTC minute", "meaning": "interface metric observation time", "role": "join key"},
        "region": {"type": "categorical", "unit": "city alias", "meaning": "observation region", "role": "join key"},
        "node": {"type": "categorical", "unit": "node id", "meaning": "interface owner", "role": "root-cause entity"},
        "node_type": {"type": "categorical", "unit": "role family", "meaning": "interface owner role family", "role": "candidate context"},
        "interface_id": {"type": "categorical", "unit": "interface id", "meaning": "observed interface", "role": "link localization"},
        "if_role": {"type": "categorical", "unit": "role label", "meaning": "interface function when available", "role": "link context"},
        "rx_bytes_rate": {"type": "float", "unit": "bytes/s", "meaning": "received byte rate", "role": "traffic evidence"},
        "tx_bytes_rate": {"type": "float", "unit": "bytes/s", "meaning": "transmitted byte rate", "role": "traffic evidence"},
        "rx_packets_rate": {"type": "float", "unit": "packets/s", "meaning": "received packet rate", "role": "traffic evidence"},
        "tx_packets_rate": {"type": "float", "unit": "packets/s", "meaning": "transmitted packet rate", "role": "traffic evidence"},
        "rx_drop_rate": {"type": "float", "unit": "drops/s", "meaning": "received packet drop rate", "role": "loss evidence"},
        "tx_drop_rate": {"type": "float", "unit": "drops/s", "meaning": "transmitted packet drop rate", "role": "loss evidence"},
        "rx_error_rate": {"type": "float", "unit": "errors/s", "meaning": "received packet error rate", "role": "loss evidence"},
        "tx_error_rate": {"type": "float", "unit": "errors/s", "meaning": "transmitted packet error rate", "role": "loss evidence"},
        "carrier_changes": {"type": "float", "unit": "count", "meaning": "interface carrier changes", "role": "link stability evidence"},
    },
    "routing_metrics": {
        "timestamp": {"type": "datetime", "unit": "UTC minute", "meaning": "routing metric observation time", "role": "join key"},
        "region": {"type": "categorical", "unit": "city alias", "meaning": "observation region", "role": "join key"},
        "node": {"type": "categorical", "unit": "node id", "meaning": "routing metric owner", "role": "root-cause entity"},
        "node_type": {"type": "categorical", "unit": "role family", "meaning": "routing metric owner role family", "role": "candidate context"},
        "metric_name": {"type": "categorical", "unit": "metric name", "meaning": "routing control-plane metric", "role": "routing signature"},
        "label": {"type": "structured string", "unit": "Prometheus-like labels", "meaning": "peer, command or route context", "role": "routing disambiguation"},
        "value": {"type": "float-or-string", "unit": "metric-specific", "meaning": "metric value", "role": "routing evidence"},
    },
    "scrape_health": {
        "timestamp": {"type": "datetime", "unit": "UTC minute", "meaning": "scrape attempt time", "role": "join key"},
        "region": {"type": "categorical", "unit": "city alias", "meaning": "observation region", "role": "join key"},
        "target_id": {"type": "categorical", "unit": "exporter target", "meaning": "scrape target address", "role": "observability entity"},
        "node": {"type": "categorical", "unit": "node id", "meaning": "scrape target owner", "role": "observability entity"},
        "exporter_type": {"type": "categorical", "unit": "exporter name", "meaning": "node or routing exporter", "role": "source context"},
        "scrape_up": {"type": "integer", "unit": "0/1", "meaning": "whether scrape succeeded", "role": "data quality evidence"},
        "scrape_duration_seconds": {"type": "float", "unit": "seconds", "meaning": "scrape duration", "role": "observability evidence"},
        "scrape_samples": {"type": "integer", "unit": "samples", "meaning": "samples returned by scrape", "role": "observability evidence"},
        "scrape_error": {"type": "string", "unit": "message", "meaning": "scrape error when present", "role": "observability evidence"},
    },
    "frr_syslog_events": {
        "id": {"type": "integer", "unit": "event id", "meaning": "event record id", "role": "record key"},
        "received_at": {"type": "datetime", "unit": "UTC", "meaning": "collector receive time", "role": "transport timing"},
        "event_time": {"type": "datetime", "unit": "UTC", "meaning": "event occurrence time", "role": "join key"},
        "received_at_raw": {"type": "datetime", "unit": "UTC", "meaning": "raw collector timestamp", "role": "audit field"},
        "event_time_raw": {"type": "datetime", "unit": "UTC", "meaning": "raw event timestamp", "role": "audit field"},
        "source_ip": {"type": "string", "unit": "IP address", "meaning": "syslog sender", "role": "entity context"},
        "hostname": {"type": "string", "unit": "hostname", "meaning": "emitting network element", "role": "root-cause entity"},
        "region": {"type": "categorical", "unit": "city alias", "meaning": "log region", "role": "join key"},
        "facility": {"type": "categorical", "unit": "syslog facility", "meaning": "syslog facility", "role": "log context"},
        "severity": {"type": "categorical", "unit": "level", "meaning": "log severity name", "role": "event weight"},
        "severity_code": {"type": "integer", "unit": "syslog code", "meaning": "numeric severity code", "role": "event weight"},
        "program": {"type": "categorical", "unit": "process", "meaning": "FRR daemon name", "role": "routing signature"},
        "pid": {"type": "integer", "unit": "process id", "meaning": "daemon process id", "role": "log context"},
        "message": {"type": "string", "unit": "free text", "meaning": "event message", "role": "routing evidence"},
        "inserted_at": {"type": "datetime", "unit": "UTC", "meaning": "database insertion time", "role": "audit field"},
    },
    "netflow_5tuple": {
        "minute_utc": {"type": "datetime", "unit": "UTC minute", "meaning": "flow aggregation minute", "role": "join key"},
        "region_code": {"type": "categorical", "unit": "city alias", "meaning": "source collection region code", "role": "join key"},
        "region": {"type": "categorical", "unit": "city label", "meaning": "source collection region", "role": "join key"},
        "node_key": {"type": "categorical", "unit": "collector node key", "meaning": "compact node identifier", "role": "entity mapping"},
        "node": {"type": "string", "unit": "node label", "meaning": "flow-observing node", "role": "root-cause entity"},
        "node_type": {"type": "categorical", "unit": "role family", "meaning": "flow-observing node role", "role": "candidate context"},
        "interface_id": {"type": "categorical", "unit": "interface id", "meaning": "flow-observing interface", "role": "link context"},
        "if_role": {"type": "categorical", "unit": "role label", "meaning": "interface function when available", "role": "link context"},
        "collector_port": {"type": "integer", "unit": "port", "meaning": "flow collector port", "role": "collector context"},
        "protocol": {"type": "integer", "unit": "IP protocol number", "meaning": "transport/network protocol number", "role": "flow context"},
        "src_addr": {"type": "string", "unit": "IP address", "meaning": "flow source address", "role": "five-tuple key"},
        "src_port": {"type": "integer", "unit": "port", "meaning": "flow source port", "role": "five-tuple key"},
        "dst_addr": {"type": "string", "unit": "IP address", "meaning": "flow destination address", "role": "five-tuple key"},
        "dst_port": {"type": "integer", "unit": "port", "meaning": "flow destination port", "role": "five-tuple key"},
        "packets": {"type": "integer", "unit": "packets/minute", "meaning": "packet count in minute", "role": "traffic evidence"},
        "bytes": {"type": "integer", "unit": "bytes/minute", "meaning": "byte count in minute", "role": "traffic evidence"},
        "flow_record_count": {"type": "integer", "unit": "records", "meaning": "underlying flow record count", "role": "aggregation context"},
        "first_seen": {"type": "datetime", "unit": "UTC", "meaning": "first packet/event time in bucket", "role": "flow timing"},
        "last_seen": {"type": "datetime", "unit": "UTC", "meaning": "last packet/event time in bucket", "role": "flow timing"},
    },
}


TRAFFIC_BASE_FIELDS = {
    "id": ("integer", "record id", "record key"),
    "timestamp_utc": ("datetime", "flow metric observation time", "join key"),
    "prometheus_sample_time_utc": ("datetime", "underlying Prometheus sample time", "audit timing"),
    "series_key": ("string", "stable metric series hash", "series key"),
    "flow_type": ("categorical", "DNS/Web/Auth/Elephant flow family", "service context"),
    "source_region": ("categorical", "probe source region", "join key"),
    "source_ip": ("string", "probe source IP", "flow context"),
    "target_region": ("categorical", "target service region", "impact localization"),
    "target_domain": ("categorical", "target service domain", "service localization"),
    "protocol": ("categorical", "application/transport protocol", "flow context"),
}


TRAFFIC_SUFFIX_MEANINGS = {
    "active": ("float", "currently active operations/flows", "current load evidence"),
    "batch_concurrency": ("float", "concurrent probe batches", "current load evidence"),
    "batches_started_total": ("float", "cumulative started batches", "counter; derive rates"),
    "batches_completed_total": ("float", "cumulative completed batches", "counter; derive rates"),
    "batches_failed_total": ("float", "cumulative failed batches", "counter; derive rates"),
    "batches_timeout_total": ("float", "cumulative timed-out batches", "counter; derive rates"),
    "requests_total": ("float", "cumulative requests", "counter; derive rates"),
    "success_total": ("float", "cumulative successful requests", "counter; derive rates"),
    "error_total": ("float", "cumulative error requests", "counter; derive rates"),
    "bytes_sent_total": ("float", "cumulative sent bytes", "counter; derive rates"),
    "bytes_received_total": ("float", "cumulative received bytes", "counter; derive rates"),
    "duration_seconds_sum": ("float", "cumulative request duration", "counter; derive rates"),
    "active_seconds_total": ("float", "cumulative active seconds", "counter; derive rates"),
    "duration_seconds_count": ("float", "number of duration observations", "counter; derive rates"),
    "batch_duration_seconds": ("float", "latest/observed batch duration", "latency evidence"),
    "last_batch_timestamp_seconds": ("float", "Unix timestamp of latest batch", "timing context"),
    "observed_qps": ("float", "observed queries per second", "service load evidence"),
    "latency_mean_seconds": ("float", "mean request latency", "service latency evidence"),
    "latency_p95_seconds": ("float", "95th percentile request latency", "service latency evidence"),
    "request_latency_seconds_sum": ("float", "request latency sum", "counter; derive rates"),
    "request_latency_seconds_count": ("float", "request latency sample count", "counter; derive rates"),
    "throughput_bps": ("float", "throughput in bits per second", "network performance evidence"),
    "retransmits_total": ("float", "cumulative retransmissions", "counter; derive rates"),
    "loss_rate": ("float", "packet/flow loss ratio", "loss evidence"),
    "jitter_seconds": ("float", "latency variation", "delay evidence"),
}


FAULT_TAXONOMY = {
    "link": ["delay", "rate_limit", "loss"],
    "firewall": ["acl_drop", "rate_limit", "port_block", "cpu_pressure", "default_route_error", "rule_order_error"],
    "resource": ["cpu_pressure", "memory_pressure", "disk_io_pressure", "disk_space_low", "process_pressure", "softirq_pressure"],
    "routing": ["blackhole", "bgp_session_down", "bgp_route_flap", "wrong_static_route", "ospf6_neighbor_down", "ospf6_cost_anomaly", "wrong_default_route"],
    "service": ["dns_down", "dns_wrong_record", "web_5xx", "web_slow", "auth_timeout", "auth_error"],
}


NETWORK_ROLES = [
    "br-1", "br-2", "cr-1", "cr-2", "fw", "traffic-vm",
    "service-vm-1", "service-vm-2", "service-vm-3", "monitor-vm",
]


SOURCE_TO_CATEGORY_WEIGHT = {
    "node_metrics": {"resource": 1.0},
    "interface_metrics": {"link": 1.0, "firewall": 0.65, "routing": 0.45},
    "routing_metrics": {"routing": 1.0},
    "traffic_flow_metrics": {"service": 1.0, "link": 0.55, "firewall": 0.55},
    "frr_syslog_events": {"routing": 1.25},
    "scrape_health": {"observability": 1.0},
    "netflow_5tuple": {"link": 0.65, "firewall": 0.65, "service": 0.65, "routing": 0.35},
}


def source_definition(source: str) -> Dict[str, object]:
    for item in SOURCE_DEFINITIONS:
        if item["source"] == source:
            return item
    raise KeyError(source)


def infer_traffic_field(field: str) -> Dict[str, str]:
    """Return a field dictionary entry for one of the repeated traffic groups."""
    if field in TRAFFIC_BASE_FIELDS:
        dtype, meaning, role = TRAFFIC_BASE_FIELDS[field]
        return {"type": dtype, "unit": "metric-specific", "meaning": meaning, "role": role}
    for prefix in ("dns_flow_", "web_flow_", "auth_flow_", "elephant_flow_"):
        if field.startswith(prefix):
            suffix = field[len(prefix):]
            if suffix.startswith("request_latency_seconds_bucket_le_"):
                return {"type": "float", "unit": "count", "meaning": f"{prefix[:-1]} request latency histogram bucket {suffix[len('request_latency_seconds_bucket_le_'):]}", "role": "latency evidence"}
            if suffix in TRAFFIC_SUFFIX_MEANINGS:
                dtype, meaning, role = TRAFFIC_SUFFIX_MEANINGS[suffix]
                unit = "ratio" if suffix == "loss_rate" else "seconds" if "seconds" in suffix or suffix == "jitter_seconds" else "metric-specific"
                return {"type": dtype, "unit": unit, "meaning": f"{prefix[:-1]} {meaning}", "role": role}
    return {"type": "unknown", "unit": "metric-specific", "meaning": "unmapped traffic metric", "role": "additional evidence"}

"""Auditable field interpretations; inferred meaning is never called verified."""

OFFICIAL_URL = 'https://challenge.aiops.cn/home/competition/2087843807868489822'


def metric_semantics(source: str, metric: str) -> dict:
    kind, unit, status, basis = 'unknown', None, 'unverified', 'No registered interpretation; preserve raw numeric scale.'
    if source == 'node':
        if metric in {'cpu_usage', 'disk_io_util'}:
            kind, unit, status = 'gauge', 'percent', 'inferred'
            basis = 'Public sample values exceed 1 and remain below 100; percent scale inferred, no conversion.'
        elif metric.endswith('_ratio'):
            kind, unit, status = 'gauge', 'fraction', 'inferred'
            basis = 'Ratio field name and observed public sample range; not an official unit definition.'
        elif metric in {'disk_read_rate', 'disk_write_rate'}:
            kind, unit, status = 'gauge', 'bytes/second', 'inferred'
            basis = 'Disk throughput field name; byte unit is a documented interpretation pending exporter validation.'
        elif metric in {'load1', 'load5', 'process_count'}:
            kind, unit, status = 'gauge', 'count' if metric == 'process_count' else 'load', 'inferred'
            basis = 'Named process count or load average; preserve observed values.'
    elif source == 'interface' and metric.endswith('_rate'):
        kind, status = 'gauge', 'inferred'
        unit = 'bytes/second' if 'bytes' in metric else 'packets/second'
        basis = 'Precomputed rate field; do not difference again. Drop/error packet units inferred from names.'
    elif source == 'netflow' and metric in {'bytes', 'packets', 'flow_record_count'}:
        kind, unit, status = 'interval_count', {'bytes': 'bytes', 'packets': 'packets', 'flow_record_count': 'exported_records'}[metric], 'inferred'
        basis = 'minute_utc CSV aggregates quantities per row; sum records, do not difference or count unique flows.'
    elif source == 'scrape' and metric in {'scrape_up', 'scrape_duration_seconds', 'scrape_samples'}:
        kind, unit, status = 'gauge', {'scrape_up': 'binary', 'scrape_duration_seconds': 'seconds', 'scrape_samples': 'samples'}[metric], 'inferred'
        basis = 'Scrape observation field meaning, retaining each target/exporter.'
    elif source in {'routing', 'traffic'}:
        if source == 'routing' and metric in {'bgp_peer_count', 'ipv6_route_count'}:
            kind, unit, status = 'gauge', 'count', 'inferred'
            basis = 'Registered routing snapshot count; _count here is not a cumulative counter.'
        elif metric.endswith(('_total', '_sum', '_count')) or '_bucket_le_' in metric:
            kind, status = 'counter', 'inferred'
            unit = 'seconds' if '_seconds_sum' in metric or 'active_seconds_total' in metric else 'bytes' if 'bytes_' in metric else 'count'
            basis = 'Prometheus cumulative naming convention; decreases invalidate the difference instead of guessing reset increments.'
        elif source == 'routing':
            units = {'bgp_peer_uptime_seconds': 'seconds', 'ospf6_neighbor_state_code': 'state_code',
                     'bgp_command_success': 'binary', 'bgp_peer_up': 'binary', 'ipv6_route_exists': 'binary',
                     'ipv6_route_nexthop_info': 'binary', 'ipv6_default_route_info': 'binary',
                     'ospf6_interface_enabled': 'binary', 'ospf6_interface_cost': 'cost',
                     'bgp_peer_prefix_received': 'prefixes', 'bgp_peer_prefix_sent': 'prefixes'}
            if metric in units or metric in {'bgp_peer_count', 'ipv6_route_count'}:
                kind, unit, status = 'gauge', units.get(metric, 'count'), 'inferred'
                basis = 'Registered routing snapshot metric; complete labels kept separately. Snapshot count is not a cumulative counter.'
        else:
            if metric.endswith('_seconds'):
                kind, unit = 'gauge', 'seconds'
            elif metric.endswith('_qps'):
                kind, unit = 'gauge', 'requests/second'
            elif metric.endswith('_bps'):
                kind, unit = 'gauge', 'bits/second'
            elif metric.endswith('_loss_rate'):
                kind, unit = 'gauge', 'fraction'
            elif metric.endswith(('_active', '_batch_concurrency')):
                kind, unit = 'gauge', 'count'
            if kind != 'unknown':
                status, basis = 'inferred', 'Business metric field naming; only fields matching the row flow_type are applicable.'
    return {'source': source, 'metric': metric, 'kind': kind, 'unit': unit, 'status': status,
            'verified': False, 'basis': basis}


def applicable_metrics(record) -> dict[str, str | None]:
    """Retain missing/bad cells as well as valid values; ignore inactive flow columns."""
    from aiops_v4.data.discovery import numeric_columns
    if record.source == 'routing':
        metric = record.raw.get('metric_name')
        return {metric: record.raw.get('value')} if metric else {}
    names = numeric_columns(record.source, list(record.raw))
    if record.source == 'traffic':
        flow = record.dimensions.get('flow_type')
        if flow in {'dns', 'web', 'auth', 'elephant'}:
            names = [name for name in names if name.startswith(flow + '_flow_')]
    return {name: record.raw.get(name) for name in names}

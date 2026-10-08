"""Select native observation grains without inferring a forwarding path."""

import json

from .identity import entity_identity
from .semantics import applicable_metrics
from .time import iso_time, observation_time

VIEWS = {'node': 'resource', 'interface': 'link', 'routing': 'routing',
         'traffic': 'traffic', 'netflow': 'traffic', 'scrape': 'collection', 'frr': 'log_context'}
_DIMENSIONS = {'node': (), 'interface': ('interface_id',),
               'traffic': ('series_key', 'flow_type', 'source_region', 'source_ip', 'target_region', 'target_domain', 'protocol'),
               'netflow': ('interface_id', 'protocol'), 'scrape': ('target_id', 'exporter_type'),
               'frr': ('program', 'severity', 'facility')}


def series_dimensions(record) -> dict:
    if record.source == 'routing':
        return {key: value for key, value in record.dimensions.items() if key != 'node_type'}
    return {name: record.dimensions.get(name, '') for name in _DIMENSIONS[record.source]}


def prepare_record(record, naive_timezone: str) -> dict:
    timestamp, time_flags = observation_time(record, naive_timezone)
    identity = entity_identity(record)
    dimensions = series_dimensions(record)
    key = json.dumps([record.batch, record.source, identity['entity_id'], dimensions],
                     ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    flags = sorted((set(record.quality_flags) - {'assumed_utc'}) | set(time_flags))
    for required in {'interface': ('interface_id',), 'routing': ('metric_name',),
                     'netflow': ('interface_id', 'protocol'), 'traffic': ('series_key', 'flow_type'),
                     'scrape': ('target_id',)}.get(record.source, ()):
        if not dimensions.get(required):
            flags.append(f'missing_dimension:{required}')
    return {'batch': record.batch, 'series_key': key, 'source': record.source, 'view': VIEWS[record.source],
            'identity': identity, 'dimensions': dimensions, 'timestamp': iso_time(timestamp),
            'raw_values': applicable_metrics(record), 'flags': sorted(set(flags)),
            'ref': {'record_id': record.record_id, 'source_file': record.source_file,
                    'record_index': record.record_index, 'line_start': record.line_start, 'line_end': record.line_end}}

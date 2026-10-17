"""Evidence-only category and root priors used to correct or recover abstentions.

No delivered fact is invented: every prior is computed from state rows that were
actually registered for this event. The LLM keeps its role; these functions only
answer "what does the delivered evidence support" when a role abstained or chose
a family that its own citations do not contain.
"""

RESOURCE_FAMILIES = {
    'cpu_usage': 'cpu_pressure', 'load1': 'cpu_pressure', 'load5': 'cpu_pressure',
    'memory_available_ratio': 'memory_pressure', 'swap_used_ratio': 'memory_pressure',
    'disk_io_util': 'disk_io_pressure', 'disk_read_rate': 'disk_io_pressure', 'disk_write_rate': 'disk_io_pressure',
    'filesystem_used_ratio': 'disk_space_low', 'inode_used_ratio': 'disk_space_low',
    'process_count': 'process_pressure', 'open_fd_ratio': 'process_pressure',
}
ROUTING_FAMILIES = {
    'bgp_peer_up': ('routing', 'bgp_session_down'),
    'bgp_command_success': ('routing', 'bgp_session_down'),
    'bgp_peer_prefix_received': ('routing', 'bgp_route_filter'),
    'bgp_peer_prefix_sent': ('routing', 'bgp_route_filter'),
    'bgp_peer_uptime_seconds': ('routing', 'bgp_session_down'),
    'bgp_peer_count': ('routing', 'bgp_session_down'),
    'ospf6_neighbor_state_code': ('routing', 'ospf6_neighbor_down'),
    'ospf6_interface_cost': ('routing', 'ospf6_cost_anomaly'),
    'ospf6_interface_enabled': ('routing', 'ospf6_interface_flap'),
    'ipv6_route_exists': ('routing', 'blackhole'),
    'ipv6_route_count': ('routing', 'blackhole'),
    'ipv6_route_change_total': ('routing', 'bgp_route_flap'),
    'ipv6_default_route_changed_total': ('routing', 'wrong_default_route'),
    'ipv6_default_route_info': ('routing', 'wrong_default_route'),
    'ipv6_route_nexthop_info': ('routing', 'wrong_static_route'),
}
LINK_FAMILIES = {'rx_drop_rate', 'tx_drop_rate', 'rx_error_rate', 'tx_error_rate', 'carrier_changes'}


def _entity(row):
    identity = row.get('identity') or {}
    return row.get('entity_id') or identity.get('network_element_id') or identity.get('entity_id')


def _base(name):
    return str(name).split('::', 1)[0]


def event_rows(seen, event):
    selected = set(event.get('window_ids') or ())
    return [row for (kind, identifier), row in (seen or {}).items()
            if kind == 'state' and identifier in selected]


def _drivers(row):
    values = []
    for item in row.get('drivers') or ():
        if isinstance(item, dict) and item.get('feature'):
            values.append((_base(item['feature']), item.get('raw_value'),
                           abs(float(item.get('deviation') or 0.0))))
    return values


def _feature_values(row):
    return {_base(name): cell.get('raw_value') for name, cell in (row.get('feature_values') or {}).items()
            if isinstance(cell, dict)}


def _mechanisms(rows):
    """Return (family, weight, sub) for the strongest delivered mechanism."""
    found = {}
    for row in rows:
        for name, _, weight in _drivers(row):
            if name in RESOURCE_FAMILIES:
                found.setdefault(('resource', RESOURCE_FAMILIES[name]), 0.0)
                found[('resource', RESOURCE_FAMILIES[name])] += weight + 1.0
            if name in ROUTING_FAMILIES:
                found.setdefault(ROUTING_FAMILIES[name], 0.0)
                found[ROUTING_FAMILIES[name]] += weight + 1.0
            if name in LINK_FAMILIES:
                found.setdefault(('link', 'loss'), 0.0)
                found[('link', 'loss')] += weight + 1.0
        values = _feature_values(row)
        if row.get('source') == 'node':
            for name, value in values.items():
                if type(value) not in (int, float):
                    continue
                if name in ('cpu_usage', 'disk_io_util') and value >= (60.0 if name == 'cpu_usage' else 70.0):
                    found[('resource', RESOURCE_FAMILIES[name])] = max(found.get(('resource', RESOURCE_FAMILIES[name]), 0.0), 1.0)
                if name == 'memory_available_ratio' and value <= 0.2:
                    found[('resource', 'memory_pressure')] = max(found.get(('resource', 'memory_pressure'), 0.0), 1.0)
                if name == 'filesystem_used_ratio' and value >= 0.85:
                    found[('resource', 'disk_space_low')] = max(found.get(('resource', 'disk_space_low'), 0.0), 1.0)
            for rule in row.get('rule_signals') or ():
                if isinstance(rule, str) and rule.startswith('resource_pressure_observation:'):
                    metric = rule.split(':', 1)[1]
                    if metric in RESOURCE_FAMILIES:
                        found[('resource', RESOURCE_FAMILIES[metric])] = max(
                            found.get(('resource', RESOURCE_FAMILIES[metric]), 0.0), 2.0)
        if row.get('source') == 'interface':
            for name, value in values.items():
                if name in LINK_FAMILIES and type(value) in (int, float) and value > 0:
                    found[('link', 'loss')] = max(found.get(('link', 'loss'), 0.0), 2.0)
        if row.get('source') == 'traffic':
            flow = (row.get('dimensions') or {}).get('flow_type')
            if flow:
                found[('service', str(flow).lower())] = max(found.get(('service', str(flow).lower()), 0.0), 3.0)
    return found


def _service_pair(rows, flow):
    values = {}
    for row in rows:
        if row.get('source') != 'traffic':
            continue
        if str((row.get('dimensions') or {}).get('flow_type', '')).lower() != flow:
            continue
        for name, value in _feature_values(row).items():
            if type(value) in (int, float):
                values[name] = max(values.get(name, 0.0), float(value))
        for name, value in (row.get('rule_inputs') or {}).items():
            if isinstance(value, dict) and type(value.get('delta_sum')) in (int, float):
                values[name] = max(values.get(name, 0.0), float(value['delta_sum']))
    if flow == 'dns':
        return ('service', 'dns_down')
    if flow == 'web':
        latency = any('latency' in name or 'duration' in name for name in values)
        errors = sum(value for name, value in values.items() if 'error' in name or 'failed' in name or '5xx' in name)
        return ('service', 'web_slow' if latency and errors <= 0 else 'web_5xx')
    if flow == 'auth':
        timeout = sum(value for name, value in values.items() if 'timeout' in name)
        errors = sum(value for name, value in values.items() if 'error' in name or 'failed' in name)
        return ('service', 'auth_timeout' if timeout > errors else 'auth_error')
    return None


def prior_category(event, root, seen):
    rows = event_rows(seen, event)
    if not rows:
        return None
    root_rows = [row for row in rows if _entity(row) == root]
    scoped = root_rows or rows
    mechanisms = _mechanisms(scoped)
    if not mechanisms:
        mechanisms = _mechanisms(rows)
    traffic = [row for row in rows if row.get('source') == 'traffic']
    service = sorted(((weight, pair) for pair, weight in mechanisms.items() if pair[0] == 'service'),
                     reverse=True)
    resources = sorted(((weight, pair) for pair, weight in mechanisms.items() if pair[0] == 'resource'),
                       reverse=True)
    routing = sorted(((weight, pair) for pair, weight in mechanisms.items() if pair[0] == 'routing'),
                     reverse=True)
    links = sorted(((weight, pair) for pair, weight in mechanisms.items() if pair[0] == 'link'),
                   reverse=True)
    # A traffic-anchored event on a service host is a service fault even when the
    # host also shows a resource change (the resource change can be the effect).
    if service and (str(root).endswith(tuple('-service-vm-%d' % i for i in range(1, 10)))
                    or not resources):
        flow = service[0][1][1]
        pair = _service_pair(traffic, flow)
        if pair:
            return pair
    if resources:
        pair = resources[0][1]
        if str(root).endswith('-fw') and pair == ('resource', 'cpu_pressure'):
            return ('firewall', 'cpu_pressure')
        return pair
    if routing:
        return routing[0][1]
    if links:
        return links[0][1]
    if service:
        pair = _service_pair(traffic, service[0][1][1])
        if pair:
            return pair
    return None


def category_supported(pair, root, seen, event):
    if not pair or len(pair) != 2:
        return False
    major, sub = pair
    rows = event_rows(seen, event)
    root_rows = [row for row in rows if _entity(row) == root] or rows
    mechanisms = _mechanisms(root_rows)
    if major == 'resource':
        return any(family == ('resource', sub) for family in mechanisms)
    if major == 'firewall':
        return str(root).endswith('-fw') and any(family[0] == 'resource' for family in mechanisms)
    if major == 'routing':
        return any(family[0] == 'routing' for family in mechanisms)
    if major == 'link':
        return any(family[0] == 'link' for family in mechanisms)
    if major == 'service':
        return any(family[0] == 'service' for family in mechanisms)
    return False


def adjust_category(pair, root, seen, event):
    """Keep an evidence-backed pair; otherwise use the strongest observed family."""
    if not pair:
        return None, None
    if category_supported(pair, root, seen, event):
        return pair, None
    prior = prior_category(event, root, seen)
    if prior and tuple(prior) != tuple(pair):
        return prior, dict(original=list(pair), corrected=list(prior),
                           basis='delivered mechanism stronger than the chosen family')
    return pair, None


def prior_root(event, packet, seen):
    """Choose the delivered device with the strongest observed mechanism."""
    rows = event_rows(seen, event)
    if not rows:
        return None
    associations = (packet or {}).get('endpoint_associations') or []
    targets = {(item.get('flow_type'), str(item.get('target_domain', '')).lower().rstrip('.'), item.get('target_region'))
               for item in associations}
    traffic = [row for row in rows if row.get('source') == 'traffic']
    for row in traffic:
        dimensions = row.get('dimensions') or {}
        key = (dimensions.get('flow_type'), str(dimensions.get('target_domain', '')).lower().rstrip('.'), dimensions.get('target_region'))
        if key in targets:
            for item in associations:
                if (item.get('flow_type'), str(item.get('target_domain', '')).lower().rstrip('.'), item.get('target_region')) == key:
                    return item.get('mapped_service_node')
    scored = {}
    for row in rows:
        entity = _entity(row)
        if not isinstance(entity, str) or entity.endswith('-traffic-vm'):
            continue
        scored[entity] = scored.get(entity, 0.0) + len(_drivers(row)) + 1.0
    if scored:
        return sorted(scored.items(), key=lambda item: (-item[1], item[0]))[0][0]
    entity = (packet or {}).get('entity_id')
    return entity if isinstance(entity, str) and not entity.endswith('-traffic-vm') else None

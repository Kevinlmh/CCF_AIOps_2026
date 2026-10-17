import importlib

import pytest


def priors():
    return importlib.import_module('aiops_v4.agents.evidence_priors')


def state(identifier, entity, source='node', drivers=(), features=None, flow=None, rule_signals=()):
    dimensions = {'flow_type': flow} if flow else {}
    return dict(vector_id=identifier, entity_id=entity, source=source,
                dimensions=dimensions, rule_signals=list(rule_signals),
                drivers=[dict(feature=name, raw_value=value, deviation=weight)
                         for name, value, weight in drivers],
                feature_values={name: dict(raw_value=value) for name, value in (features or {}).items()})


def event(*identifiers):
    return dict(window_ids=list(identifiers))


def test_node_cpu_driver_selects_cpu_pressure():
    rows = {('state', 'a'): state('a', 'xian-br-1', drivers=[('cpu_usage::mean', 91.2, 398.0)])}
    assert priors().prior_category(event('a'), 'xian-br-1', rows) == ('resource', 'cpu_pressure')


def test_memory_and_disk_drivers_map_to_their_own_subclasses():
    rows = {('state', 'm'): state('m', 'xian-br-1', drivers=[('memory_available_ratio::mean', 0.04, 20.0)]),
            ('state', 'd'): state('d', 'xian-br-1', drivers=[('disk_io_util::mean', 97.0, 30.0)])}
    assert priors().prior_category(event('m'), 'xian-br-1', rows) == ('resource', 'memory_pressure')
    assert priors().prior_category(event('d'), 'xian-br-1', rows) == ('resource', 'disk_io_pressure')


def test_firewall_host_maps_cpu_pressure_to_firewall_major():
    rows = {('state', 'f'): state('f', 'xian-fw', drivers=[('cpu_usage::mean', 88.0, 12.0)])}
    assert priors().prior_category(event('f'), 'xian-fw', rows) == ('firewall', 'cpu_pressure')


def test_traffic_only_event_on_service_host_is_service_category():
    rows = {('state', 't'): state('t', 'beida-traffic-vm', source='traffic', flow='dns',
                                  features={'dns_flow_error_total': 12.0})}
    assert priors().prior_category(event('t'), 'beida-service-vm-1', rows) == ('service', 'dns_down')


def test_web_latency_without_errors_is_slow_and_errors_are_5xx():
    slow = {('state', 'w'): state('w', 'shanghai-traffic-vm', source='traffic', flow='web',
                                  features={'web_flow_latency_p95_seconds': 2.5})}
    failed = {('state', 'e'): state('e', 'shanghai-traffic-vm', source='traffic', flow='web',
                                   features={'web_flow_error_total': 3.0})}
    assert priors().prior_category(event('w'), 'shanghai-service-vm-1', slow) == ('service', 'web_slow')
    assert priors().prior_category(event('e'), 'shanghai-service-vm-1', failed) == ('service', 'web_5xx')


def test_auth_timeout_is_separated_from_auth_error():
    timeout = {('state', 't'): state('t', 'wuhan-traffic-vm', source='traffic', flow='auth',
                                    features={'auth_flow_batches_timeout_total': 4.0,
                                              'auth_flow_error_total': 1.0})}
    assert priors().prior_category(event('t'), 'wuhan-service-vm-1', timeout) == ('service', 'auth_timeout')


def test_service_category_is_unsupported_without_any_traffic_observation():
    rows = {('state', 'c'): state('c', 'xian-br-1', drivers=[('cpu_usage::mean', 80.0, 9.0)])}
    assert priors().category_supported(('service', 'web_5xx'), 'xian-br-1', rows, event('c')) is False
    assert priors().category_supported(('resource', 'cpu_pressure'), 'xian-br-1', rows, event('c')) is True


def test_unsupported_category_is_corrected_and_supported_category_is_kept():
    rows = {('state', 'c'): state('c', 'xian-br-1', drivers=[('cpu_usage::mean', 80.0, 9.0)])}
    pair, adjustment = priors().adjust_category(('service', 'web_5xx'), 'xian-br-1', rows, event('c'))
    assert pair == ('resource', 'cpu_pressure') and adjustment['original'] == ['service', 'web_5xx']
    pair, adjustment = priors().adjust_category(('resource', 'cpu_pressure'), 'xian-br-1', rows, event('c'))
    assert pair == ('resource', 'cpu_pressure') and adjustment is None


def test_no_delivered_evidence_yields_no_prior():
    assert priors().prior_category(event('missing'), 'xian-br-1', {}) is None
    assert priors().prior_root(event('missing'), {}, {}) is None


def test_prior_root_uses_endpoint_association_before_client_probe():
    packet = dict(entity_id='beida-traffic-vm', endpoint_associations=[dict(
        flow_type='dns', target_domain='dns.beida.aiops.local', target_region='beida',
        mapped_service_node='beida-service-vm-1')])
    rows = {('state', 't'): state('t', 'beida-traffic-vm', source='traffic', flow='dns',
                                  features={'dns_flow_error_total': 1.0})}
    rows[('state', 't')]['dimensions'].update(target_domain='dns.beida.aiops.local', target_region='beida')
    assert priors().prior_root(event('t'), packet, rows) == 'beida-service-vm-1'


def test_prior_root_prefers_the_device_with_the_strongest_mechanism():
    rows = {('state', 'a'): state('a', 'xian-br-1', drivers=[('bgp_peer_up::last', 0.0, 40.0)]),
            ('state', 'b'): state('b', 'xian-cr-1', drivers=[('cpu_usage::mean', 70.0, 2.0)])}
    assert priors().prior_root(event('a', 'b'), {}, rows) == 'xian-br-1'

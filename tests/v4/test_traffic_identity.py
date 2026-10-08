"""Explicit auxiliary traffic identity must not share a candidate counter chain."""

import json

from test_records import read, write_csv
from test_semantics import feature_module


def test_traffic_source_role_does_not_override_explicit_auxiliary_entity(tmp_path):
    rows,_=read(tmp_path,'traffic_flow_metrics.csv',[{
        'timestamp_utc':'2026-09-17T04:00:00Z','source_region':'xian','node':'probe-vm',
        'flow_type':'web','series_key':'a','web_flow_requests_total':'10',
    }])
    assert rows[0].node_id is None
    assert 'unmapped_entity' in rows[0].quality_flags
    assert 'entity_from_source_role' not in rows[0].quality_flags


def test_auxiliary_and_real_traffic_entity_counters_do_not_cross(tmp_path):
    root,output=tmp_path/'input',tmp_path/'output'
    write_csv(root,'traffic_flow_metrics.csv',[
        {'timestamp_utc':f'2026-09-17T04:0{minute}:00Z','source_region':'xian','node':node,
         'flow_type':'web','series_key':'a','web_flow_requests_total':value}
        for minute,node,value in [(0,'probe-vm','10'),(1,'traffic-vm','25')]
    ])
    feature_module('pipeline').build_features(root,'stage2',output,naive_timezone='UTC')
    windows=[json.loads(line) for line in (output/'windows.jsonl').read_text().splitlines()]
    identities={row['identity']['entity_id']:row for row in windows}
    assert set(identities)=={'aux:xian:probe-vm','xian-traffic-vm'}
    assert identities['aux:xian:probe-vm']['identity']['candidate'] is False
    assert identities['xian-traffic-vm']['identity']['candidate'] is True
    assert all(row['metrics']['web_flow_requests_total']['delta_sum'] is None for row in windows)

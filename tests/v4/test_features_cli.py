"""End-to-end CSV sorting, artifacts, explicit policy and output protection."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_records import write_csv
from test_semantics import feature_module

REPO = Path(__file__).resolve().parents[2]


def run(*arguments):
    return subprocess.run([sys.executable, '-m', 'aiops_v4.features', *map(str,arguments)],
                          cwd=REPO,capture_output=True,text=True)


def build(root, output, **kwargs):
    return feature_module('pipeline').build_features(root,'stage2',output,naive_timezone='UTC',**kwargs)


def get_windows(output):
    return [json.loads(line) for line in (output/'windows.jsonl').read_text().splitlines()]


def test_cross_file_unsorted_counters_are_sorted_before_differencing(tmp_path):
    root, output = tmp_path/'input',tmp_path/'output'
    fields={'node':'br1','metric_name':'ipv6_route_change_total','label':''}
    write_csv(root,'routing_metrics_a.csv',[{**fields,'timestamp':'2026-09-17T04:02:00Z','value':'40'}])
    write_csv(root,'routing_metrics_b.csv',[
        {**fields,'timestamp':'2026-09-17T04:01:00Z','value':'25'},
        {**fields,'timestamp':'2026-09-17T04:00:00Z','value':'10'},
    ])
    summary=build(root,output)
    rows=get_windows(output)
    assert summary['rows_scanned']==summary['rows_accepted']==3
    assert summary['window_count']==len(rows)==3 and summary['complete']
    assert [row['metrics']['ipv6_route_change_total']['delta_sum'] for row in rows]==[None,15,15]
    assert (output/'semantics.csv').exists() and (output/'report.md').exists()
    assert not any(path.name.startswith('.v4-window-') for path in tmp_path.iterdir())


def test_all_sources_and_auxiliary_nodes_survive_feature_build(tmp_path):
    root,output=tmp_path/'input',tmp_path/'output'
    fixtures=[
        ('node_metrics.csv',{'timestamp':'2026-09-17 04:00:00','node':'probe-vm','cpu_usage':'0'}),
        ('interface_metrics.csv',{'timestamp':'2026-09-17 04:00:00','node':'br1','interface_id':'ens4','rx_bytes_rate':'10'}),
        ('routing_metrics.csv',{'timestamp':'2026-09-17 04:00:00','node':'br1','metric_name':'bgp_peer_up','label':'peer="a"','value':'0'}),
        ('netflow.csv',{'minute_utc':'2026-09-17 04:00:00','node_key':'br1','interface_id':'ens4','protocol':'6','bytes':'100'}),
        ('traffic_flow_metrics.csv',{'timestamp_utc':'2026-09-17 04:00:00','source_region':'xian','flow_type':'web','series_key':'a','web_flow_observed_qps':'0'}),
        ('scrape_health.csv',{'timestamp':'2026-09-17 04:00:00','node':'br1','target_id':'a','exporter_type':'node','scrape_up':'0'}),
        ('frr_syslog_events.csv',{'event_time':'2026-09-17 04:00:00','hostname':'br1','program':'bgpd','severity':'error','message':'full\nlog'}),
    ]
    for name,row in fixtures: write_csv(root,name,[row])
    summary=build(root,output)
    rows=get_windows(output)
    assert summary['rows_accepted']==len(rows)==7
    assert {row['view'] for row in rows}=={'resource','link','routing','traffic','collection','log_context'}
    auxiliary=next(row for row in rows if row['source']=='node')
    assert not auxiliary['identity']['candidate'] and auxiliary['metrics']['cpu_usage']['mean']==0
    assert next(row for row in rows if row['source']=='frr')['event_count']==1


def test_bad_time_is_retained_in_rejected_output_and_summary(tmp_path):
    root,output=tmp_path/'input',tmp_path/'output'
    write_csv(root,'node_metrics.csv',[{'timestamp':'broken','node':'br1','cpu_usage':'10'}])
    summary=build(root,output)
    assert summary['rows_scanned']==summary['rows_rejected']==1 and summary['rows_accepted']==0
    rejected=json.loads((output/'rejected.jsonl').read_text())
    assert rejected['record']['raw']['cpu_usage']=='10' and rejected['reason']
    assert get_windows(output)==[]


def test_prefix_scope_and_existing_outputs_are_protected(tmp_path):
    root,output=tmp_path/'input',tmp_path/'output'
    write_csv(root,'node_metrics.csv',[
        {'timestamp':f'2026-09-17T04:0{minute}:00Z','node':'br1','cpu_usage':'10'} for minute in range(3)
    ])
    summary=build(root,output,max_rows_per_file=1)
    assert summary['mode']=='prefix_sample' and not summary['complete']
    assert summary['rows_scanned']==1 and not summary['files'][0]['complete']
    before=(output/'summary.json').read_bytes()
    with pytest.raises(FileExistsError): build(root,output)
    assert (output/'summary.json').read_bytes()==before


def test_malformed_csv_does_not_publish_partial_features(tmp_path):
    root,output=tmp_path/'input',tmp_path/'output'
    root.mkdir()
    (root/'node_metrics.csv').write_text('timestamp,node,node\na,b,c\n')
    with pytest.raises(ValueError,match='duplicate'): build(root,output)
    assert not output.exists()
    assert not any(path.name.startswith('.v4-window-') for path in tmp_path.iterdir())


def test_cli_build_honors_timezone_and_requires_explicit_policy(tmp_path):
    root,output=tmp_path/'input',tmp_path/'output'
    write_csv(root,'node_metrics.csv',[{'timestamp':'2026-09-17 12:00:00','node':'br1','cpu_usage':'10'}])
    missing=run('build','--root',root,'--batch','stage2','--output-dir',output)
    assert missing.returncode==2 and not output.exists()
    result=run('build','--root',root,'--batch','stage2','--output-dir',output,'--naive-timezone','Asia/Shanghai')
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['window_count']==1
    assert get_windows(output)[0]['start_time']=='2026-09-17T04:00:00.000000Z'


@pytest.mark.parametrize('options',[{'window_seconds':0},{'expected_step_seconds':7},{'counter_max_gap_seconds':0}])
def test_invalid_budget_cannot_publish_output(tmp_path,options):
    root,output=tmp_path/'input',tmp_path/'output'
    write_csv(root,'node_metrics.csv',[{'timestamp':'2026-09-17 04:00:00','node':'br1'}])
    with pytest.raises(ValueError): build(root,output,**options)
    assert not output.exists()


def test_output_cannot_be_inside_input(tmp_path):
    write_csv(tmp_path,'node_metrics.csv',[{'timestamp':'2026-09-17 04:00:00','node':'br1'}])
    with pytest.raises(ValueError,match='input'): build(tmp_path,tmp_path/'output')
    assert not (tmp_path/'output').exists()

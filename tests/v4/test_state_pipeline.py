import json
import subprocess
import sys
from pathlib import Path
import pytest
from state_helpers import artifacts, state_module, window


def run(tmp_path, rows, **kwargs):
    source=artifacts(tmp_path/'input',rows)
    output=tmp_path/'output'
    summary=state_module('pipeline').discover_states(source,'stage2',output,**kwargs)
    states=[json.loads(line) for line in (output/'states.jsonl').read_text().splitlines()]
    events=[json.loads(line) for line in (output/'events.jsonl').read_text().splitlines()]
    models=[json.loads(line) for line in (output/'models.jsonl').read_text().splitlines()]
    return summary,states,events,models


@pytest.mark.parametrize('mode',['statistics','cluster','hybrid'])
def test_held_out_spike_is_traceable_and_merged_in_each_mode(tmp_path,mode):
    rows=[window(i,value=0 if i<12 or i==14 else 100) for i in range(15)]
    summary,states,events,models=run(tmp_path,list(reversed(rows)),mode=mode,
                                   reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z')
    assert len(events)==1
    event=events[0]
    assert event['start_time']=='2026-09-17T04:12:00.000000Z'
    assert event['end_time']=='2026-09-17T04:14:00.000000Z'
    assert event['window_ids']==['w12','w13']
    assert event['references'][0]['record_id']=='r12'
    assert models[0]['reference']['observed_reference_rows']==12
    assert models[0]['reference']['features']['cpu_usage::mean']['median']==0
    assert summary['candidate_events']==1 and summary['states']==15
    assert all(not state['candidate'] for state in states[:12])


def test_later_values_cannot_change_explicit_reference(tmp_path):
    rows=[window(i,value=10 if i<12 else 1000) for i in range(40)]
    _,_,_,models=run(tmp_path,rows,reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z')
    assert models[0]['reference']['features']['cpu_usage::mean']['median']==10
    assert models[0]['reference']['eligible_reference_rows']==12


def test_rules_work_without_enough_reference_and_keep_auxiliary_identity(tmp_path):
    w=window(source='scrape',metric='scrape_up',value=0,
             identity={'entity_id':'aux:xian:probe-vm','network_element_id':None,'candidate':False,'city':'xian','role':'probe-vm'})
    w['metrics']['scrape_up']['semantics']['unit']='binary'
    _,states,events,_=run(tmp_path,[w])
    assert states[0]['scoring_status']=='insufficient_reference'
    assert 'rule:collection_unavailable:scrape_up' in states[0]['triggers']
    assert events[0]['identity']['candidate'] is False


def test_counter_quality_issue_alone_never_becomes_candidate(tmp_path):
    w=window(source='traffic',metric='web_flow_error_total',value=50)
    w['metrics']['web_flow_error_total'].update(semantics={'kind':'counter','unit':'count','status':'inferred'},
                                                rate_mean=2,delta_sum=120,counter_decrease_count=1)
    _,states,events,_=run(tmp_path,[w])
    assert events==[]
    assert states[0]['quality_signals']==['counter_decrease:web_flow_error_total']


def test_normal_window_and_time_gap_end_candidate_event(tmp_path):
    rows=[]
    for i,value in [(0,0),(1,1),(2,0),(4,0)]:
        w=window(i,source='scrape',metric='scrape_up',value=value)
        w['metrics']['scrape_up']['semantics']['unit']='binary';rows.append(w)
    _,_,events,_=run(tmp_path,rows)
    assert [event['window_ids'] for event in events]==[['w0'],['w2'],['w4']]


def test_long_event_caps_evidence_without_capping_duration(tmp_path):
    rows=[]
    for i in range(70):
        w=window(i,source='scrape',metric='scrape_up',value=0)
        w['metrics']['scrape_up']['semantics']['unit']='binary';rows.append(w)
    _,_,events,_=run(tmp_path,rows)
    assert len(events)==1 and events[0]['window_count']==70
    assert len(events[0]['window_ids'])==64 and len(events[0]['references'])==16
    assert events[0]['window_ids_truncated'] and events[0]['references_truncated']
    assert events[0]['end_time']=='2026-09-17T05:10:00.000000Z'


@pytest.mark.parametrize('fault',['duplicate','overlap','mixed','nan','wrong_count'])
def test_bad_inputs_publish_nothing_and_preserve_sources(tmp_path,fault):
    rows=[window(0),window(1)]
    if fault=='duplicate':rows[1]['window_id']='w0'
    if fault=='overlap':rows[0]['end_time']='2026-09-17T04:02:00Z'
    if fault=='mixed':rows[1]['batch']='stage1'
    if fault=='nan':rows[0]['metrics']['cpu_usage']['mean']=float('nan')
    source=artifacts(tmp_path/'input',rows)
    if fault=='wrong_count':
        s=json.loads((source/'summary.json').read_text());s['window_count']=50;(source/'summary.json').write_text(json.dumps(s))
    before={p.name:p.read_bytes() for p in source.iterdir()}
    with pytest.raises(ValueError):state_module('pipeline').discover_states(source,'stage2',tmp_path/'output')
    assert not (tmp_path/'output').exists()
    assert {p.name:p.read_bytes() for p in source.iterdir()}==before
    assert not list(tmp_path.glob('.v4-state-*'))


def test_existing_output_is_not_overwritten(tmp_path):
    source=artifacts(tmp_path/'input',[window()]);output=tmp_path/'output';output.mkdir()
    (output/'keep').write_text('unchanged')
    with pytest.raises(FileExistsError):state_module('pipeline').discover_states(source,'stage2',output)
    assert (output/'keep').read_text()=='unchanged'


@pytest.mark.parametrize('kwargs',[{'reference_start':'2026-09-17T04:00:00Z'},
                                  {'stat_threshold':float('nan')},{'min_reference':1}])
def test_invalid_reference_or_threshold_is_rejected(tmp_path,kwargs):
    source=artifacts(tmp_path/'input',[window()])
    with pytest.raises(ValueError):state_module('pipeline').discover_states(source,'stage2',tmp_path/'output',**kwargs)
    assert not (tmp_path/'output').exists()


def test_cli_outside_checkout_preserves_sampling_scope(tmp_path):
    source=artifacts(tmp_path/'input',[window()],mode='prefix_sample')
    proc=subprocess.run([sys.executable,'-m','aiops_v4.states','discover','--windows-dir',str(source),'--batch','stage2',
                         '--output-dir',str(tmp_path/'output')],cwd=tmp_path,text=True,capture_output=True)
    assert proc.returncode==0,proc.stderr
    assert json.loads(proc.stdout)['input_scope']['mode']=='prefix_sample'
    assert json.loads((tmp_path/'output'/'summary.json').read_text())['reference_scope']['known_healthy'] is False


@pytest.mark.parametrize('location',['summary','unknown_metric'])
def test_exponent_overflow_in_unused_fields_is_rejected_before_publication(tmp_path,location):
    source=artifacts(tmp_path/'input',[window()])
    if location=='summary':
        path=source/'summary.json'
        path.write_text(path.read_text()[:-1]+',"unused":1e400}')
    else:
        path=source/'windows.jsonl'
        w=window();w['metrics']['mystery']={'semantics':{'kind':'unknown','unit':None,'status':'unverified'},'mean':'EXPONENT'}
        path.write_text(json.dumps(w).replace('"EXPONENT"','1e400')+'\n')
    before=path.read_bytes()
    with pytest.raises(ValueError,match='nonfinite'):
        state_module('pipeline').discover_states(source,'stage2',tmp_path/'output')
    assert not (tmp_path/'output').exists() and path.read_bytes()==before


def test_held_out_semantic_change_cannot_create_false_deviation(tmp_path):
    rows=[window(i,value=10) for i in range(13)]
    rows[-1]['metrics']['cpu_usage']['semantics']['unit']='count'
    source=artifacts(tmp_path/'input',rows)
    with pytest.raises(ValueError,match='semantics'):
        state_module('pipeline').discover_states(source,'stage2',tmp_path/'output',
            reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z')
    assert not (tmp_path/'output').exists()


@pytest.mark.parametrize('source,metric,trigger',[('scrape','scrape_up','collection_unavailable'),
                                                 ('routing','bgp_peer_up','protocol_indicator_zero')])
def test_transient_binary_zero_after_recovery_still_has_evidence(tmp_path,source,metric,trigger):
    rows=[]
    for i in range(13):
        w=window(i,source=source,metric=metric,value=1)
        w['metrics'][metric]['semantics']['unit']='binary'
        if i==12:w['metrics'][metric].update(mean=2/3,min=0,max=1,last=1,change_count=2,used_sample_count=3)
        rows.append(w)
    _,states,events,_=run(tmp_path,rows,reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z')
    assert states[-1]['stat_score'] is None and states[-1]['cluster_distance']==0
    assert f'rule:{trigger}:{metric}' in states[-1]['triggers']
    assert f'observed_binary_change:{metric}' in states[-1]['rule_signals']
    assert events[0]['window_ids']==['w12']


def test_adjacent_binary_recovery_is_recorded_and_ends_zero_event(tmp_path):
    rows=[]
    for i,value in enumerate([0,1]):
        w=window(i,source='scrape',metric='scrape_up',value=value)
        w['metrics']['scrape_up']['semantics']['unit']='binary';rows.append(w)
    _,states,events,_=run(tmp_path,rows)
    assert 'observed_binary_change:scrape_up' in states[1]['rule_signals']
    assert states[1]['candidate'] is False and events[0]['window_ids']==['w0']

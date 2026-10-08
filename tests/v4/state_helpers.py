"""Small literal window fixtures; no production expectations derived here."""
import importlib
import json
from datetime import datetime, timedelta, timezone
import pytest


def state_module(name):
    try:
        return importlib.import_module('aiops_v4.states.' + name)
    except ModuleNotFoundError as error:
        pytest.fail(f'state discovery implementation missing: {error}')


def window(index=0, source='node', metric='cpu_usage', value=10, **changes):
    start=datetime(2026,9,17,4,tzinfo=timezone.utc)+timedelta(minutes=index)
    w={'schema_version':1,'window_id':f'w{index}', 'batch':'stage2','source':source,
       'view':{'node':'resource','interface':'link','routing':'routing','scrape':'collection','netflow':'traffic','traffic':'traffic','frr':'log_context'}[source],
       'identity':{'entity_id':'xian-br-1','network_element_id':'xian-br-1','candidate':True,'city':'xian','role':'br-1'},
       'dimensions':{},'start_time':start.isoformat(),'end_time':(start+timedelta(minutes=1)).isoformat(),
       'coverage':{'fraction':1.0},'quality_flags':[], 'references_truncated':False,
       'references':[{'record_id':f'r{index}','source_file':'input.csv','record_index':index+1,'line_start':index+2,'line_end':index+2}],
       'event_count':None,
       'metrics':{metric:{'semantics':{'kind':'gauge','unit':'percent','status':'inferred','verified':False},
                           'mean':value,'last':value,'rate_mean':None,'sum':None,'input_count':1,'used_sample_count':1,
                           'invalid_input_count':0,'conflicting_time_count':0,'counter_decrease_count':0,'counter_gap_count':0}}}
    w.update(changes)
    return w


def artifacts(path, rows, *, mode='full'):
    path.mkdir()
    (path/'windows.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    (path/'summary.json').write_text(json.dumps({'schema_version':1,'batch':'stage2','window_count':len(rows),
                                               'mode':mode,'complete':mode=='full','naive_timezone':'UTC',
                                               'timezone_officially_confirmed':False}))
    return path

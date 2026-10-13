from contextlib import closing
"""Query plans at real merge boundaries must remain bounded by native groups."""
from pathlib import Path
import sqlite3
import pytest

from test_evidence_pipeline import inputs
from aiops_v4.features.build import build_feature_store
from aiops_v4.states import build
from aiops_v4.storage import load_json


@pytest.mark.parametrize('resume_checkpoint',[False,True])
def test_event_membership_merge_searches_native_group_before_all_groups_finish(tmp_path,monkeypatch,resume_checkpoint):
    raw,_,_=inputs(tmp_path,interfaces=3)
    dataset=tmp_path/'dataset';features=dataset/'features';states=dataset/'states'
    build_feature_store(raw,'stage2',features,naive_timezone='UTC',workers=1,min_free_bytes=0,netflow_streaming=False)
    actual_connect=sqlite3.connect;plans=[]

    class MergeConnection(sqlite3.Connection):
        def execute(self,sql,parameters=(),/):
            if sql.startswith('INSERT INTO event_windows SELECT'):
                plan=list(super().execute('EXPLAIN QUERY PLAN '+sql,parameters))
                plans.append(plan)
                assert any('SEARCH deltas' in row[3] for row in plan),plan
                assert not any('SCAN deltas' in row[3] for row in plan),plan
            return super().execute(sql,parameters)

    def connect(database,*args,**kwargs):
        if Path(str(database)).name=='.states.sqlite':kwargs['factory']=MergeConnection
        return actual_connect(database,*args,**kwargs)

    monkeypatch.setattr(build.sqlite3,'connect',connect)
    settings=dict(reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z',workers=1,min_free_bytes=0)
    if resume_checkpoint:
        def interrupt(value):
            if value.get('event')=='group_complete':raise KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            build.build_state_store(features,'stage2',states,progress=interrupt,**settings)
        # An earlier unsealed checkpoint has committed groups but no lookup index.
        with closing(actual_connect(states/'.states.sqlite')) as db:
            with db:db.execute('DROP INDEX delta_group_time')
        summary=build.build_state_store(features,'stage2',states,resume=True,**settings)
    else:
        summary=build.build_state_store(features,'stage2',states,**settings)
    assert summary['states']==64 and summary['candidate_windows']==11 and summary['candidate_events']==7
    assert len(plans)==7
    with actual_connect(states/'states.sqlite') as db:
        assert db.execute('SELECT count(*) FROM event_windows').fetchone()[0]==11
        for identifier,payload in db.execute('SELECT id,payload FROM events'):
            event=load_json(payload)
            members={row[0].hex() for row in db.execute('SELECT id FROM event_windows WHERE event=?',(identifier,))}
            assert members==set(event['window_ids'])
            assert len(members)==event['window_count']

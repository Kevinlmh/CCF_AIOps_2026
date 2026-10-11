import json
from importlib import import_module,util
import shutil
import pytest
from test_evidence_pipeline import inputs
from test_feature_store import compare
from aiops_v4.features.build import build_feature_store


def api():
    assert util.find_spec('aiops_v4.states.build'), 'linked state builder not implemented'
    return import_module('aiops_v4.states.build'),import_module('aiops_v4.states.store')


@pytest.mark.parametrize('workers',[1,2])
def test_state_deltas_and_models_reconstruct_identical_candidates_and_vectors(tmp_path,workers):
    raw,_,old=inputs(tmp_path,interfaces=3)
    dataset=tmp_path/'dataset';features=dataset/'features'
    build_feature_store(raw,'stage2',features,naive_timezone='UTC',workers=workers,min_free_bytes=0,netflow_streaming=False)
    build,store=api();new=dataset/'states'
    summary=build.build_state_store(features,'stage2',new,reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z',workers=workers,min_free_bytes=0)
    assert summary['states']==64 and summary['candidate_events']==7
    assert 'input_windows_sha256' not in summary and len(summary['input_feature_store_sha256'])==64
    with store.StateStore(new/'states.sqlite') as db:
        for name in ('states','matrix','models','events'):
            expected=[json.loads(l) for l in (old/(name+'.jsonl')).read_text().splitlines()]
            compare(expected,list(db.rows(name)))
    assert not (new/'matrix.jsonl').exists()
    moved=tmp_path/'moved';shutil.move(dataset,moved)
    with store.StateStore(moved/'states/states.sqlite') as db:assert len(list(db.rows('states')))==64
    with pytest.raises(ValueError):
        (moved/'features/features.sqlite').write_bytes(b'changed')
        store.StateStore(moved/'states/states.sqlite')


def test_completed_groups_survive_interruption_and_state_policy_changes_invalidate_cache(tmp_path):
    raw,_,_=inputs(tmp_path,interfaces=0);features=tmp_path/'dataset/features'
    build_feature_store(raw,'stage2',features,naive_timezone='UTC',workers=1,min_free_bytes=0)
    build,store=api();out=tmp_path/'dataset/states'
    def interrupt(value):
        if value.get('event')=='group_complete':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):build.build_state_store(features,'stage2',out,workers=1,min_free_bytes=0,progress=interrupt)
    notifications=[]
    summary=build.build_state_store(features,'stage2',out,workers=1,min_free_bytes=0,resume=True,progress=notifications.append)
    assert summary['states']==61 and any(r['event']=='group_reused' for r in notifications)
    with pytest.raises(ValueError,match='changed'):build.build_state_store(features,'stage2',out,workers=1,min_free_bytes=0,resume=True,stat_threshold=9)

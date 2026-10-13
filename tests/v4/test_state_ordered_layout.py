"""State physical ordering remains separate from external IDs and tool semantics."""
import json
from contextlib import closing
import sqlite3
import pytest
from aiops_v4.states import build,codec
from aiops_v4.states.store import StateStore,GRAIN
from aiops_v4.features.build import build_feature_store
from aiops_v4.features.store import FeatureStore
from aiops_v4.storage import db_dump,load_json
from test_evidence_pipeline import inputs
from test_feature_store import compare


def features(tmp_path):
    raw,_,old=inputs(tmp_path,interfaces=3)
    out=tmp_path/'dataset/features'
    build_feature_store(raw,'stage2',out,naive_timezone='UTC',workers=1,min_free_bytes=0,netflow_streaming=False)
    return out,old


def settings():
    return dict(reference_start='2026-09-17T04:00:00Z',reference_end='2026-09-17T04:12:00Z',workers=1,min_free_bytes=0)


def test_state_writer_uses_integer_physical_order_and_narrow_status_without_vacuum(tmp_path,monkeypatch):
    feature_dir,old=features(tmp_path);actual_connect=sqlite3.connect;statements=[]
    class TraceConnection(sqlite3.Connection):
        pass
    def connect(database,*args,**kwargs):
        db=actual_connect(database,*args,**kwargs)
        if str(database).endswith('/.states.sqlite'):db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(build.sqlite3,'connect',connect)
    out=feature_dir.parent/'states';summary=build.build_state_store(feature_dir,'stage2',out,**settings())
    with actual_connect(out/'states.sqlite') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==3
        columns={row[1]:row for row in db.execute('PRAGMA table_info(deltas)')}
        assert columns['seq'][2:]==('INTEGER',0,None,1)
        assert columns['grp_order'][2]=='INTEGER' and columns['status'][2]=='INTEGER'
        assert db.execute('SELECT seq FROM deltas ORDER BY seq').fetchall()==[(i,) for i in range(1,65)]
        assert db.execute('SELECT count(*) FROM deltas WHERE payload IS NOT NULL').fetchone()[0]==0
        from aiops_v4.states.blocks import unpack_block
        assert all(payload.startswith(codec.STATE_RAW_MAGIC) for row in db.execute('SELECT payload FROM state_blocks') for payload in unpack_block(row[0]))
        assert db.execute('SELECT count(DISTINCT id) FROM deltas').fetchone()[0]==64
        assert db.execute('SELECT count(*) FROM event_windows').fetchone()[0]==11
    assert not any(statement.strip().upper()=='VACUUM' for statement in statements)
    with StateStore(out/'states.sqlite') as state:
        for name in ('states','matrix','models','events'):
            compare([json.loads(line) for line in (old/(name+'.jsonl')).read_text().splitlines()],list(state.rows(name)))
    assert summary['states']==64


def test_worker_persists_typed_local_dictionary_instead_of_json_deltas(tmp_path):
    feature_dir,_=features(tmp_path)
    with FeatureStore(feature_dir/'features.sqlite') as reader:
        group,identifier=reader.db.execute('SELECT state_group,id FROM groups ORDER BY state_group LIMIT 1').fetchone()
        options=dict(reference_start=None,reference_end=None,min_reference=12,clusters=3,mode='hybrid',stat_threshold=6.,
            cluster_threshold=3.,rare_fraction=.1,minimum_overlap=.5,rules_enabled=True,candidate_policy='legacy',candidate_min_windows=2)
        path=tmp_path/'worker.sqlite';build.compute_group((group,identifier,options,str(path)),reader)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==3
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='strings'").fetchone()
        assert db.execute('SELECT count(*) FROM states WHERE payload IS NOT NULL').fetchone()[0]==0
        from aiops_v4.states.blocks import unpack_block
        assert all(payload.startswith(codec.STATE_RAW_MAGIC) for row in db.execute('SELECT payload FROM state_blocks') for payload in unpack_block(row[0]))


def test_incompatible_private_state_checkpoint_is_rejected_without_schema_changes(tmp_path):
    feature_dir,_=features(tmp_path);out=feature_dir.parent/'states'
    def interrupt(event):
        if event.get('event')=='group_complete':raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):build.build_state_store(feature_dir,'stage2',out,progress=interrupt,**settings())
    target=out/'.states.sqlite'
    with closing(sqlite3.connect(target)) as db:db.execute('PRAGMA user_version=1');db.commit()
    before=target.read_bytes()
    with pytest.raises(ValueError,match='format|checkpoint'):
        build.build_state_store(feature_dir,'stage2',out,resume=True,**settings())
    assert target.read_bytes()==before


@pytest.mark.parametrize('typed',[False,True])
def test_legacy_state_store_reads_json_and_dictionary_payloads_without_rescoring(tmp_path,typed):
    feature_dir,_=features(tmp_path);out=feature_dir.parent/'states'
    build.build_state_store(feature_dir,'stage2',out,**settings())
    with StateStore(out/'states.sqlite') as reader:
        original=list(reader.rows('states'));matrices=list(reader.rows('matrix'))
    old=feature_dir.parent/'legacy';old.mkdir();target=old/'states.sqlite'
    source=sqlite3.connect(out/'states.sqlite');db=sqlite3.connect(target)
    db.executescript("""PRAGMA user_version=1;
      CREATE TABLE metadata(key TEXT PRIMARY KEY,payload BLOB);
      CREATE TABLE models(grp TEXT PRIMARY KEY,payload BLOB);
      CREATE TABLE deltas(id BLOB PRIMARY KEY,grp INTEGER,grp_order TEXT,start INTEGER,end INTEGER,candidate INTEGER,status TEXT,quality INTEGER,suppressed INTEGER,payload BLOB) WITHOUT ROWID;
      CREATE TABLE events(id TEXT PRIMARY KEY,entity TEXT,grp TEXT,grp_order TEXT,start INTEGER,end INTEGER,payload BLOB);
      CREATE TABLE event_windows(id BLOB PRIMARY KEY,event TEXT) WITHOUT ROWID;
    """)
    for row in source.execute('SELECT key,payload FROM metadata'):db.execute('INSERT INTO metadata VALUES (?,?)',row)
    db.executemany('INSERT INTO models VALUES (?,?)',source.execute('SELECT grp,payload FROM models'))
    writer=codec.DeltaWriter(db)
    by_id={bytes.fromhex(state['vector_id']):state for state in original}
    for row in source.execute('SELECT id,grp,start,end,candidate,quality,suppressed FROM deltas'):
        ident,grp,start,end,candidate,quality,suppressed=row;state=by_id[ident]
        delta={k:v for k,v in state.items() if k not in GRAIN}
        payload=writer.pack(delta) if typed else db_dump(delta)
        db.execute('INSERT INTO deltas VALUES (?,?,?,?,?,?,?,?,?,?)',(ident,grp,state['group_id'],start,end,candidate,state['scoring_status'],quality,suppressed,payload))
    db.commit();db.close();source.close()
    with StateStore(target,verify=False) as reader:
        compare(original,list(reader.rows('states')));compare(matrices,list(reader.rows('matrix')))
        assert reader.pair(original[0]['vector_id'])[0]==original[0]



def test_mid_group_import_rolls_back_dictionary_rows_and_progress_then_resumes(tmp_path,monkeypatch):
    feature_dir,old=features(tmp_path);out=feature_dir.parent/'states'
    actual_connect=sqlite3.connect;snapshot={};groups=[0]
    class FailingConnection(sqlite3.Connection):
        def executemany(self,sql,parameters,/):
            if sql.startswith('INSERT INTO deltas('):
                groups[0]+=1
                if groups[0]==2:
                    row=next(iter(parameters));super().execute(sql,row)
                    raise RuntimeError('injected import interruption')
            return super().executemany(sql,parameters)
    def connect(database,*args,**kwargs):
        if str(database).endswith('/.states.sqlite'):kwargs['factory']=FailingConnection
        return actual_connect(database,*args,**kwargs)
    def progress(event):
        if event.get('event')=='group_complete' and not snapshot:
            with closing(actual_connect(out/'.states.sqlite')) as db:
                snapshot.update({table:db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                    for table in ('deltas','models','events','event_windows','strings','progress')})
                assert db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    monkeypatch.setattr(build.sqlite3,'connect',connect)
    with pytest.raises(RuntimeError,match='injected'):
        build.build_state_store(feature_dir,'stage2',out,progress=progress,**settings())
    with closing(actual_connect(out/'.states.sqlite')) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert {table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in snapshot}==snapshot
    monkeypatch.setattr(build.sqlite3,'connect',actual_connect)
    build.build_state_store(feature_dir,'stage2',out,resume=True,**settings())
    with StateStore(out/'states.sqlite') as reader:
        compare([json.loads(line) for line in (old/'states.jsonl').read_text().splitlines()],list(reader.rows('states')))
    assert not any((out/(name+suffix)).exists() for name in ('.states.sqlite','states.sqlite') for suffix in ('-wal','-shm','-journal'))



def test_state_final_indexes_remain_inside_resource_policy_guard(tmp_path,monkeypatch):
    from contextlib import contextmanager
    feature_dir,_=features(tmp_path);out=feature_dir.parent/'states'
    original_guard=build.ResourcePolicy.guard_sql;actual_connect=sqlite3.connect;inside=[0]
    @contextmanager
    def guard(self,db):
        with original_guard(self,db):
            inside[0]+=1
            try:yield
            finally:inside[0]-=1
    class GuardConnection(sqlite3.Connection):
        def execute(self,sql,parameters=(),/):
            if sql.startswith('CREATE INDEX IF NOT EXISTS'):
                assert inside[0]>0, 'index creation lacks resource/cancellation guard'
            return super().execute(sql,parameters)
    def connect(database,*args,**kwargs):
        if str(database).endswith('/.states.sqlite'):kwargs['factory']=GuardConnection
        return actual_connect(database,*args,**kwargs)
    monkeypatch.setattr(build.ResourcePolicy,'guard_sql',guard)
    monkeypatch.setattr(build.sqlite3,'connect',connect)
    build.build_state_store(feature_dir,'stage2',out,**settings())


def test_interruption_at_publication_keeps_all_group_progress_resumable(tmp_path,monkeypatch):
    feature_dir,_=features(tmp_path);out=feature_dir.parent/'states';original=build.finalize_build_connection
    def interrupt(db):
        assert not db.in_transaction
        assert db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
        assert db.execute('PRAGMA synchronous').fetchone()[0]==2
        assert db.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM deltas').fetchone()[0]==64
        raise RuntimeError('injected publication interruption')
    monkeypatch.setattr(build,'finalize_build_connection',interrupt)
    with pytest.raises(RuntimeError,match='injected publication'):
        build.build_state_store(feature_dir,'stage2',out,**settings())
    assert (out/'.states.sqlite').exists() and not (out/'states.sqlite').exists()
    with closing(sqlite3.connect(out/'.states.sqlite')) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT count(*) FROM progress').fetchone()[0]==db.execute('SELECT count(*) FROM models').fetchone()[0]
        assert load_json(db.execute("SELECT payload FROM metadata WHERE key='summary'").fetchone()[0])['states']==64
    monkeypatch.setattr(build,'finalize_build_connection',original);progress=[]
    build.build_state_store(feature_dir,'stage2',out,resume=True,progress=progress.append,**settings())
    assert progress and all(event['event']=='group_reused' for event in progress)
    assert (out/'states.sqlite').exists() and not (out/'.states.sqlite').exists()

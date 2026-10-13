import sqlite3
from types import SimpleNamespace
from aiops_v4.features.write_codec import PreparedWindowWriter
from aiops_v4.states import build

def test_prepared_worker_schema_and_rows_share_one_transaction(tmp_path):
    db=sqlite3.connect(tmp_path/'prepared.sqlite')
    try:
        PreparedWindowWriter(db,'probe')
        assert db.in_transaction, 'worker DDL commits every table separately'
        db.rollback()
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='windows'").fetchone()
        assert db.execute('PRAGMA user_version').fetchone()[0]==0
    finally:db.close()

def test_state_worker_schema_uses_one_group_transaction(tmp_path,monkeypatch):
    observed=[]
    original=sqlite3.connect
    class Tracking(sqlite3.Connection):
        def executescript(self,script):
            result=super().executescript(script)
            if 'CREATE TABLE states' in script:observed.append(self.in_transaction)
            return result
    def connect(*args,**kwargs):
        kwargs['factory']=Tracking
        return original(*args,**kwargs)
    monkeypatch.setattr(build.sqlite3,'connect',connect)
    features=SimpleNamespace(summary={'batch':'probe'},windows=lambda *a,**k:iter(()))
    settings=dict(reference_start=None,reference_end=None,min_reference=12,clusters=3,mode='hybrid',stat_threshold=8,cluster_threshold=4,rare_fraction=0,minimum_overlap=.5,rules_enabled=True,candidate_policy='legacy',candidate_min_windows=2)
    build.compute_group(('group',1,settings,str(tmp_path/'states.sqlite')),features,None)
    assert observed==[True], 'state worker DDL performs separate durable commits'
    with original(tmp_path/'states.sqlite') as db:
        assert db.execute('SELECT count(*) FROM metadata').fetchone()[0]==2

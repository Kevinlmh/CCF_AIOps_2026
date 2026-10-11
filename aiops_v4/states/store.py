"""State deltas linked to canonical windows; matrices are derived in pages."""
from collections import OrderedDict
from pathlib import Path
import sqlite3
from aiops_v4.storage import load_json,db_dump
from aiops_v4.data.cache import verify_layer,dependency_path
from aiops_v4.features.store import FeatureStore,micros,timestamp
from .matrix import window_vector
from .codec import unpack_delta

GRAIN=('vector_id','group_id','batch','source','view','identity','dimensions','start_time','end_time','references','references_truncated')


class StateStore:
    def __init__(self,path,*,verify=True):
        self.path=Path(path).resolve(strict=True)
        if verify:verify_layer(self.path.parent)
        self.db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
        self.db.execute('PRAGMA query_only=ON');self.db.execute('PRAGMA cache_size=-8192')
        if self.db.execute('PRAGMA user_version').fetchone()[0]!=1:raise ValueError('unsupported state format')
        self.summary=load_json(self.db.execute("SELECT payload FROM metadata WHERE key='summary'").fetchone()[0])
        relative=load_json(self.db.execute("SELECT payload FROM metadata WHERE key='feature_dependency'").fetchone()[0])
        self.features=FeatureStore(dependency_path(self.path.parent,relative),verify=verify)
        self.group_ids={i:g for i,g in self.features.db.execute('SELECT id,state_group FROM groups')}
        self.cache=OrderedDict()
        self.strings={i:s for i,s in self.db.execute('SELECT id,value FROM strings')} if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='strings'").fetchone() else {}

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
    def close(self):self.db.close();self.features.db.close()

    def reconstruct(self,records):
        records=list(records)
        windows=self.features.get_windows([r[0].hex() for r in records]);result=[]
        for identifier,grp,delta in records:
            vector=window_vector(windows[identifier.hex()],self.summary['batch']);extra=unpack_delta(delta,self.strings)
            vector['in_reference']=extra['is_reference']
            state={k:vector[k] for k in GRAIN};state.update(extra)
            result.append((state,vector))
        return result

    def pair(self,identifier):
        if identifier not in self.cache:
            row=self.db.execute('SELECT id,grp,payload FROM deltas WHERE id=?',(bytes.fromhex(identifier),)).fetchone()
            if row is None:raise ValueError('unknown state ID')
            self.cache[identifier]=self.reconstruct([row])[0]
            if len(self.cache)>256:self.cache.popitem(last=False)
        return self.cache[identifier]

    def rows(self,kind,*,page_size=256):
        if kind in ('states','matrix'):
            cursor=self.db.execute('SELECT id,grp,payload FROM deltas ORDER BY grp_order,start,id')
            while True:
                page=cursor.fetchmany(page_size)
                if not page:break
                for state,vector in self.reconstruct(page):yield state if kind=='states' else vector
        elif kind=='models':
            yield from (load_json(r[0]) for r in self.db.execute('SELECT payload FROM models ORDER BY grp'))
        elif kind=='events':
            yield from (load_json(r[0]) for r in self.db.execute('SELECT payload FROM events ORDER BY grp_order,start,id'))
        else:raise ValueError('unknown state artifact')

    def attach_views(self,db):
        """Compatibility for bounded legacy SQL consumers; read-only attachments."""
        db.execute('ATTACH DATABASE ? AS upstream_states',(self.path.as_uri()+'?mode=ro',))
        db.execute('ATTACH DATABASE ? AS upstream_features',(self.features.path.as_uri()+'?mode=ro',))
        db.create_function('v4_iso',1,timestamp,deterministic=True)
        db.create_function('v4_state',1,lambda i:db_dump(self.pair(i)[0]))
        db.create_function('v4_matrix',1,lambda i:db_dump(self.pair(i)[1]))
        db.executescript('''
          CREATE TEMP VIEW states AS SELECT lower(hex(d.id)) AS id,g.entity AS entity,g.state_group AS grp,g.source AS source,
            v4_iso(d.start) AS start,v4_iso(d.end) AS end,d.candidate,d.status,d.quality,v4_state(lower(hex(d.id))) AS payload
            FROM upstream_states.deltas d JOIN upstream_features.groups g ON g.id=d.grp;
          CREATE TEMP VIEW matrices AS SELECT lower(hex(id)) AS id,v4_matrix(lower(hex(id))) AS payload FROM upstream_states.deltas;
          CREATE TEMP VIEW models AS SELECT grp,payload FROM upstream_states.models;
          CREATE TEMP VIEW events AS SELECT id,entity,grp,v4_iso(start) AS start,v4_iso(end) AS end,payload FROM upstream_states.events;
          CREATE TEMP VIEW event_windows AS SELECT lower(hex(id)) AS id,event FROM upstream_states.event_windows;
          CREATE TEMP VIEW suppressed_states AS SELECT lower(hex(id)) AS id FROM upstream_states.deltas WHERE suppressed=1;
        ''')

    def query_states(self,*,entity_id=None,source=None,group_id=None,start=None,end=None,limit=100,offset=0):
        sql='SELECT d.id,d.grp,d.payload FROM deltas d JOIN features.groups g ON g.id=d.grp';where=[];args=[]
        for key,column in ((entity_id,'g.entity'),(source,'g.source'),(group_id,'g.state_group')):
            if key is not None:where.append(column+'=?');args.append(key)
        if start is not None:where.append('d.end>?');args.append(micros(start))
        if end is not None:where.append('d.start<?');args.append(micros(end))
        if where:sql+=' WHERE '+' AND '.join(where)
        sql+=' ORDER BY d.start,g.source,g.state_group,d.id LIMIT ? OFFSET ?'
        if not getattr(self,'_query_attached',False):
            self.db.execute('ATTACH DATABASE ? AS features',(self.features.path.as_uri()+'?mode=ro',));self._query_attached=True
        return self.reconstruct(self.db.execute(sql,(*args,limit,offset)))

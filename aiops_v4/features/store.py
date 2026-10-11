"""Versioned canonical windows: dictionaries, float64 statistics and anchor links."""
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import zlib

from aiops_v4.storage import db_dump, load_json
from aiops_v4.data.position import record_at

INTS = ('count','numeric_count','missing_count','non_numeric_count','nonfinite_count','zero_count',
        'quantile_sample_count','quantile_capacity','distinct_count_lower_bound','input_count',
        'missing_input_count','invalid_input_count','finite_input_count','used_sample_count',
        'duplicate_input_count','conflicting_time_count','change_count','rate_sample_count',
        'counter_decrease_count','counter_gap_count','counter_first_sample_count')
FLOATS = ('min','max','mean','std','median','mad','p01','p05','q25','q75','p95','p99',
          'missing_fraction','first','last','slope_per_second','sum','delta_sum','rate_mean','rate_min','rate_max')
BOOLS = ('quantiles_exact','distinct_exact')
FIELDS = INTS + FLOATS + BOOLS
LAYOUT = struct.Struct('<BQQ'+'q'*len(INTS)+'d'*len(FLOATS)+'B'*len(BOOLS))
ANCHOR = struct.Struct('<5Q')
EPOCH = datetime(1970,1,1,tzinfo=timezone.utc)


def micros(stamp):
    delta=datetime.fromisoformat(stamp.replace('Z','+00:00'))-EPOCH
    return (delta.days*86400+delta.seconds)*1000000+delta.microseconds


def timestamp(value):
    return (EPOCH+timedelta(microseconds=value)).isoformat(timespec='microseconds').replace('+00:00','Z')


def pack_statistics(value):
    present=nonnull=0;values=[]
    for index,key in enumerate(FIELDS):
        if key in value: present |= 1<<index
        item=value.get(key)
        if item is not None: nonnull |= 1<<index
        values.append(0 if item is None else item)
    metadata={k:v for k,v in value.items() if k not in FIELDS}
    return LAYOUT.pack(1,present,nonnull,*values),metadata


def unpack_statistics(blob,metadata):
    version,present,nonnull,*values=LAYOUT.unpack(blob)
    if version!=1: raise ValueError('unsupported statistics layout')
    result=dict(metadata)
    for index,(key,value) in enumerate(zip(FIELDS,values)):
        if present & (1<<index): result[key]=(bool(value) if key in BOOLS else value) if nonnull & (1<<index) else None
    return result


def create_store(path):
    db=sqlite3.connect(path)
    db.executescript('''PRAGMA foreign_keys=ON; PRAGMA cache_size=-8192; PRAGMA temp_store=FILE;
      PRAGMA user_version=2;
      CREATE TABLE metadata(key TEXT PRIMARY KEY,payload BLOB NOT NULL);
      CREATE TABLE groups(id INTEGER PRIMARY KEY,series TEXT UNIQUE,state_group TEXT UNIQUE,entity TEXT,source TEXT,payload BLOB NOT NULL);
      CREATE TABLE definitions(id INTEGER PRIMARY KEY,payload BLOB NOT NULL);
      CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT UNIQUE,payload BLOB NOT NULL);
      CREATE TABLE flags(id INTEGER PRIMARY KEY,payload BLOB NOT NULL);
      CREATE TABLE extras(id INTEGER PRIMARY KEY,payload BLOB NOT NULL);
      CREATE TABLE windows(id BLOB PRIMARY KEY,grp INTEGER REFERENCES groups(id),start INTEGER,end INTEGER,
        records INTEGER,observed INTEGER,truncated INTEGER,flags INTEGER REFERENCES flags(id),statistics BLOB NOT NULL,extras BLOB NOT NULL) WITHOUT ROWID;
      CREATE TABLE anchors(id INTEGER PRIMARY KEY,digest BLOB UNIQUE,file INTEGER REFERENCES files(id),payload BLOB NOT NULL);
      CREATE TABLE window_refs(window BLOB REFERENCES windows(id),position INTEGER,anchor INTEGER REFERENCES anchors(id),PRIMARY KEY(window,position)) WITHOUT ROWID;
      CREATE TABLE progress(grp TEXT PRIMARY KEY);
    ''')
    return db


class StoreWriter:
    def __init__(self,db,batch,files):
        self.db,self.batch=db,batch
        self.groups={};self.definitions={};self.flags={};self.extras={};self.file_ids={}
        for i,f in enumerate(files,1):
            db.execute('INSERT INTO files VALUES (?,?,?)',(i,f['path'],db_dump(f)));self.file_ids[f['path']]=i

    def _intern(self,table,cache,value):
        key=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        if key not in cache:
            ident=len(cache)+1;cache[key]=ident
            self.db.execute(f'INSERT INTO {table} VALUES (?,?)',(ident,db_dump(value)))
        return cache[key]

    def add(self,window,anchors):
        from aiops_v4.states.matrix import window_vector
        grain={k:window[k] for k in ('schema_version','batch','source','view','identity','dimensions','reference_limit')}
        series=json.dumps([self.batch,window['source'],window['identity']['entity_id'],window['dimensions']],ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if series not in self.groups:
            group=len(self.groups)+1;self.groups[series]=group
            state_group=window_vector(window,self.batch)['group_id']
            self.db.execute('INSERT INTO groups VALUES (?,?,?,?,?,?)',(group,series,state_group,window['identity']['entity_id'],window['source'],db_dump(grain)))
        group=self.groups[series]
        pieces=[struct.pack('<H',len(window['metrics']))]
        for name,metric in window['metrics'].items():
            numeric,extra=pack_statistics(metric)
            definition=self._intern('definitions',self.definitions,dict(name=name,extra=extra))
            pieces.extend((struct.pack('<I',definition),numeric))
        flags=self._intern('flags',self.flags,window['quality_flags'])
        extra={k:v for k,v in window.items() if k not in {*grain,'window_id','start_time','end_time','record_count','metrics','coverage','quality_flags','references','references_truncated'}}
        extra['coverage']={k:v for k,v in window['coverage'].items() if k!='observed_timestamps'}
        identifier=bytes.fromhex(window['window_id'])
        self.db.execute('INSERT INTO windows VALUES (?,?,?,?,?,?,?,?,?,?)',
            (identifier,group,micros(window['start_time']),micros(window['end_time']),window['record_count'],
             window['coverage']['observed_timestamps'],window['references_truncated'],flags,zlib.compress(b''.join(pieces),1),
             self._intern('extras',self.extras,extra)))
        for position,ref in enumerate(window['references']):
            anchor=anchors[ref['record_id']];digest=bytes.fromhex(ref['record_id'])
            self.db.execute('INSERT OR IGNORE INTO anchors(digest,file,payload) VALUES (?,?,?)',
                (digest,self.file_ids[ref['source_file']],ANCHOR.pack(*(anchor[k] for k in
                    ('record_index','line_start','line_end','byte_start','byte_length')))))
            a=self.db.execute('SELECT id FROM anchors WHERE digest=?',(digest,)).fetchone()[0]
            self.db.execute('INSERT INTO window_refs VALUES (?,?,?)',(identifier,position,a))


class FeatureStore:
    def __init__(self,path,*,verify=True):
        self.path=Path(path).resolve(strict=True)
        if verify:
            from aiops_v4.data.cache import verify_layer
            verify_layer(self.path.parent)
        self.db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
        self.db.execute('PRAGMA query_only=ON');self.db.execute('PRAGMA cache_size=-8192')
        self.version=self.db.execute('PRAGMA user_version').fetchone()[0]
        if self.version not in (1,2): raise ValueError('unsupported feature format')
        self.summary=load_json(self.db.execute("SELECT payload FROM metadata WHERE key='summary'").fetchone()[0])
        self.groups={r[0]:load_json(r[1]) for r in self.db.execute('SELECT id,payload FROM groups')}
        self.definitions={r[0]:load_json(r[1]) for r in self.db.execute('SELECT id,payload FROM definitions')}
        self.flags={r[0]:load_json(r[1]) for r in self.db.execute('SELECT id,payload FROM flags')}
        self.extras={r[0]:load_json(r[1]) for r in self.db.execute('SELECT id,payload FROM extras')} if self.version==2 else {}
        self.files={r[0]:load_json(r[1]) for r in self.db.execute('SELECT id,payload FROM files')}

    def decode_anchor(self,digest,file,payload):
        if self.version==1:return load_json(payload)
        fields=('record_index','line_start','line_end','byte_start','byte_length')
        return dict(record_id=digest.hex(),source_file=self.files[file]['path'],**dict(zip(fields,ANCHOR.unpack(payload))))

    def __enter__(self):return self
    def __exit__(self,*_):self.db.close()

    def _decode(self,row,refs):
        ident,grp,start,end,count,observed,truncated,flags,blob,extra=row
        result=dict(self.groups[grp]);extras=dict(self.extras[extra]) if self.version==2 else load_json(extra);coverage=dict(extras.pop('coverage'))
        coverage['observed_timestamps']=observed
        result.update(extras,window_id=ident.hex(),start_time=timestamp(start),end_time=timestamp(end),record_count=count,
                      coverage=coverage,quality_flags=self.flags[flags],references=refs,references_truncated=bool(truncated))
        payload=zlib.decompress(blob);n=struct.unpack_from('<H',payload)[0];offset=2;metrics={}
        for _ in range(n):
            definition=struct.unpack_from('<I',payload,offset)[0];offset+=4
            d=self.definitions[definition];metrics[d['name']]=unpack_statistics(payload[offset:offset+LAYOUT.size],d['extra']);offset+=LAYOUT.size
        if offset!=len(payload):raise ValueError('invalid statistics payload')
        result['metrics']=metrics
        return result

    def decode_rows(self,rows):
        rows=list(rows)
        if not rows:return []
        identifiers=[r[0] for r in rows];refs={i:[] for i in identifiers}
        # One query per page rather than one per window/reference.
        marks=','.join('?' for _ in identifiers)
        for window,digest,file,payload in self.db.execute(f'SELECT r.window,a.digest,a.file,a.payload FROM window_refs r JOIN anchors a ON a.id=r.anchor WHERE r.window IN ({marks}) ORDER BY r.window,r.position',identifiers):
            anchor=self.decode_anchor(digest,file,payload)
            refs[window].append({k:anchor[k] for k in ('record_id','source_file','record_index','line_start','line_end')})
        return [self._decode(r,refs[r[0]]) for r in rows]

    def windows(self,group=None,*,page_size=256):
        sql='SELECT w.* FROM windows w JOIN groups g ON g.id=w.grp';args=[]
        if group is not None:sql+=' WHERE g.state_group=?';args=[group]
        sql+=' ORDER BY '+('w.start,w.id' if group else "(g.source='netflow'),g.series,w.start,w.id")
        cursor=self.db.execute(sql,args)
        while True:
            rows=cursor.fetchmany(page_size)
            if not rows:break
            yield from self.decode_rows(rows)

    def get_windows(self,identifiers):
        values=[bytes.fromhex(i) for i in identifiers];results={}
        for start in range(0,len(values),256):
            page=values[start:start+256];marks=','.join('?' for _ in page)
            for row in self.decode_rows(self.db.execute(f'SELECT * FROM windows WHERE id IN ({marks})',page)):
                results[row['window_id']]=row
        return results

    def anchor(self,identifier):
        row=self.db.execute('SELECT digest,file,payload FROM anchors WHERE digest=?',(bytes.fromhex(identifier),)).fetchone()
        if row is None:raise ValueError('unknown/unregistered raw reference')
        return self.decode_anchor(*row),self.files[row[1]]

    def raw_record(self,identifier):
        anchor,manifest=self.anchor(identifier)
        return record_at(self.summary['input_root'],self.summary['batch'],manifest,anchor).to_dict()


def export_windows(path,destination):
    with FeatureStore(path) as store, Path(destination).open('x',encoding='utf-8') as out:
        for row in store.windows():out.write(json.dumps(row,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')

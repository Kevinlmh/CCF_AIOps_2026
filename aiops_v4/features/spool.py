"""Private resumable file spools; NetFlow never writes one sort row per flow."""
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime,timezone
import json
import math
from pathlib import Path
import sqlite3
from time import monotonic

from aiops_v4.data.position import PositionReader
from aiops_v4.data.essential import EssentialProfile
from aiops_v4.data.discovery import TIME_FIELDS
from aiops_v4.data.reader import is_missing
from aiops_v4.data.resources import ResourcePolicy
from aiops_v4.storage import db_dump,load_json
from .series import prepare_record,series_dimensions,VIEWS
from .identity import entity_identity
from .semantics import applicable_metrics
from .time import observation_time,iso_time,time_zone
from .netflow import NetflowAccumulator


def _bounded(cache,key,create,capacity=2048):
    value=cache.get(key)
    if value is None:
        value=create();cache[key]=value
        if len(cache)>capacity:cache.popitem(last=False)
    return value


class PreparedParser:
    """Cache repeated identity/time/grain; full content IDs only for retained flows."""
    def __init__(self,reader,naive_timezone,netflow_streaming=True):
        self.reader=reader;self.zone=naive_timezone
        self.netflow_streaming=netflow_streaming
        self.times=OrderedDict();self.grains=OrderedDict()
        self.numeric=reader.normalizer._numeric

    def parse(self,position):
        if self.reader.file.source!='netflow' or not self.netflow_streaming:
            record=position.record(self.reader);row=prepare_record(record,self.zone)
            row['ref'].update(byte_start=position.byte_start,byte_length=position.byte_length)
            return row
        raw=position.raw(self.reader.columns)
        time_key=tuple(raw.get(n) for n in TIME_FIELDS['netflow'])
        def parse_time(cells):
            dummy=self.reader.normalizer._normalize(cells,(),'',1,1)
            stamp,time_flags=observation_time(dummy,self.zone)
            return iso_time(stamp),sorted((set(dummy.quality_flags)-{'assumed_utc','unmapped_entity','short_csv_row'})|set(time_flags))
        primary=TIME_FIELDS['netflow'][0]
        # A valid primary timestamp makes all secondary fields irrelevant. If
        # it is invalid/missing (or violates the timezone policy), preserve the
        # complete legacy fallback parser, including its quality flags/errors.
        try:
            stamp,time_flags=_bounded(self.times,('primary',raw.get(primary)),lambda:parse_time({primary:raw.get(primary)}))
        except ValueError:
            stamp,time_flags=_bounded(self.times,('fallback',*time_key),lambda:parse_time({n:raw.get(n) for n in TIME_FIELDS['netflow']}))
        fields=('region_code','region','node_key','node','hostname','source_region','source_ip','target_id','interface_id','protocol')
        grain_key=tuple(raw.get(n) for n in fields)
        def grain():
            meta={n:raw.get(n) for n in fields};meta['minute_utc']='2000-01-01T00:00:00Z'
            record=self.reader.normalizer._normalize(meta,(),'',1,1)
            identity=entity_identity(record);dimensions=series_dimensions(record)
            key=json.dumps([self.reader.batch,'netflow',identity['entity_id'],dimensions],ensure_ascii=False,sort_keys=True,separators=(',',':'))
            flags=set(record.quality_flags)-{'short_csv_row','missing_timestamp'}
            for n in ('interface_id','protocol'):
                if not dimensions.get(n):flags.add('missing_dimension:'+n)
            return dict(batch=self.reader.batch,series_key=key,source='netflow',view=VIEWS['netflow'],identity=identity,dimensions=dimensions),flags
        template,flags=_bounded(self.grains,grain_key,grain)
        flags=set(flags)|set(time_flags)
        if len(position.cells)<len(self.reader.columns):flags.add('short_csv_row')
        if len(position.cells)>len(self.reader.columns):flags.add('extra_csv_cells')
        values={}
        for name in self.numeric:
            value=raw.get(name)
            if is_missing(value): values[name]=None;continue
            try:number=float(value)
            except (ValueError,TypeError):values[name]=value;flags.add('invalid_numeric:'+name);continue
            if not math.isfinite(number):values[name]=value;flags.add('nonfinite_numeric:'+name)
            else:values[name]=number
        ref=dict(record_id=f'position:{self.reader.file.relative_path}:{position.record_index}',source_file=self.reader.file.relative_path,
                 record_index=position.record_index,line_start=position.line_start,line_end=position.line_end,
                 byte_start=position.byte_start,byte_length=position.byte_length,_cells=position.cells)
        return dict(template,timestamp=stamp,raw_values=values,flags=sorted(flags),ref=ref)


class FastFlows(NetflowAccumulator):
    def add(self,row):
        stamp=datetime.fromisoformat(row['timestamp'].replace('Z','+00:00'))
        # Cache the bucket for repeated timestamps in this file.
        if getattr(self,'last_stamp',None)!=row['timestamp']:
            from .time import window_start
            self.last_stamp=row['timestamp'];self.last_bucket=iso_time(window_start(stamp,self.width))
        key=row['series_key'],self.last_bucket
        value=self.cache.pop(key,None)
        if value is None:
            saved=self.db.execute('SELECT payload FROM flow_windows WHERE series=? AND start=?',key).fetchone()
            value=load_json(saved[0]) if saved else dict(template=row,count=0,flags=[],refs=[],metrics={})
        value['count']+=1
        if row['flags']:value['flags']=sorted(set(value['flags'])|set(row['flags']))
        ref=dict(time=row['timestamp'],ref=row['ref']);order=lambda r:(r['time'],r['ref']['source_file'],r['ref']['record_index'])
        if len(value['refs'])<8 or order(ref)<order(value['refs'][-1]):value['refs']=sorted(value['refs']+[ref],key=order)[:8]
        if value.get('last_time_seen')!=row['timestamp']:
            self.db.execute('INSERT OR IGNORE INTO flow_times VALUES (?,?,?)',(*key,row['timestamp']));value['last_time_seen']=row['timestamp']
        for name,raw in row['raw_values'].items():
            m=value['metrics'].setdefault(name,dict(inputs=0,missing=0,invalid=0,n=0,zeros=0,mean=0.,m2=0.,total=0.,min=None,max=None,first=None,last=None))
            m['inputs']+=1
            if raw is None or (isinstance(raw,str) and is_missing(raw)):m['missing']+=1;continue
            if type(raw) in (int,float):number=raw
            else:
                try:number=float(raw)
                except (ValueError,TypeError):number=float('nan')
            if not math.isfinite(number) or number<0:m['invalid']+=1;continue
            m['n']+=1;m['zeros']+=number==0
            delta=number-m['mean'];m['mean']+=delta/m['n']
            if m['m2'] is not None:
                m['m2']+=delta*(number-m['mean'])
                if not math.isfinite(m['m2']):m['m2']=None
            if m['total'] is not None:
                m['total']+=number
                if not math.isfinite(m['total']):m['total']=None
            m['min']=number if m['min'] is None else min(m['min'],number);m['max']=number if m['max'] is None else max(m['max'],number)
            point=[row['timestamp'],row['ref']['source_file'],row['ref']['record_index'],number]
            if m['first'] is None or point[:3]<m['first'][:3]:m['first']=point
            if m['last'] is None or point[:3]>=m['last'][:3]:m['last']=point
        self.cache[key]=value
        if len(self.cache)>self.capacity:
            old_key,old_value=self.cache.popitem(last=False);self._save(old_key,old_value)

    def flush(self):
        for key,value in self.cache.items():self._save(key,value)
        self.cache.clear()


def merge_flow(a,b):
    """Merge fixed file-order moments, never completion-order contributions."""
    a['count']+=b['count'];a['flags']=sorted(set(a['flags'])|set(b['flags']))
    a['refs']=sorted(a['refs']+b['refs'],key=lambda r:(r['time'],r['ref']['source_file'],r['ref']['record_index']))[:8]
    for name,n in b['metrics'].items():
        if name not in a['metrics']:a['metrics'][name]=n;continue
        m=a['metrics'][name];old=m['n'];total=old+n['n'];delta=n['mean']-m['mean']
        if total:
            m['mean']=(m['mean']+delta*(n['n']/total)) if old else n['mean']
            m['m2']=None if m['m2'] is None or n['m2'] is None else m['m2']+n['m2']+(delta*delta*(old*n['n']/total) if old and n['n'] else 0)
            if m['m2'] is not None and not math.isfinite(m['m2']):m['m2']=None
        for key in ('inputs','missing','invalid','n','zeros'):m[key]+=n[key]
        m['total']=None if m['total'] is None or n['total'] is None else m['total']+n['total']
        if m['total'] is not None and not math.isfinite(m['total']):m['total']=None
        for key,fn in (('min',min),('max',max)):
            if n[key] is not None:m[key]=n[key] if m[key] is None else fn(m[key],n[key])
        if n['first'] is not None and (m['first'] is None or n['first'][:3]<m['first'][:3]):m['first']=n['first']
        if n['last'] is not None and (m['last'] is None or n['last'][:3]>=m['last'][:3]):m['last']=n['last']
    return a


def parse_file(task,progress=None):
    file,batch,path,settings=task;path=Path(path)
    policy=ResourcePolicy(settings['workspace_root'],settings['min_free_bytes'],settings['max_workspace_bytes'],settings['memory_bytes'],settings['workers'])
    policy.worker_limit()
    db=sqlite3.connect(path);db.execute('PRAGMA cache_size=-8192');db.execute('PRAGMA temp_store=FILE')
    try:
        db.executescript('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,payload BLOB); CREATE TABLE IF NOT EXISTS observations(series TEXT,time TEXT,idx INTEGER,payload BLOB);')
        saved=db.execute("SELECT payload FROM metadata WHERE key='checkpoint'").fetchone();saved=load_json(saved[0]) if saved else None
        done=db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()
        if done:
            if progress:progress(dict(event='file_reused',path=file.relative_path))
            return load_json(done[0])
        profile=EssentialProfile(saved['profile'] if saved else None)
        flows=None
        if file.source=='netflow' and settings['netflow_streaming']:
            if not saved:
                # DDL may survive an interruption before the first transaction
                # checkpoint. With no committed position, discard only this
                # file's private, uncommitted accumulator and start at byte 0.
                db.execute('DELETE FROM observations')
                db.execute('DROP TABLE IF EXISTS flow_windows');db.execute('DROP TABLE IF EXISTS flow_times')
                flows=FastFlows(db,settings['window_seconds'],settings['expected_step_seconds'],capacity=512)
            else:
                flows=FastFlows.__new__(FastFlows);flows.db=db;flows.width=settings['window_seconds'];flows.step=settings['expected_step_seconds'];flows.capacity=512;flows.cache=OrderedDict()
        with PositionReader(file,batch,settings['max_rows_per_file'],saved['reader'] if saved else None) as reader:
            parser=PreparedParser(reader,settings['naive_timezone'],settings['netflow_streaming']);last=monotonic();checkpoint_index=reader.rows_scanned
            if progress and saved:progress(dict(event='parse_resumed',path=file.relative_path,record_index=reader.rows_scanned))
            def commit():
                nonlocal last,checkpoint_index
                if flows:flows.flush()
                value=dict(reader=reader.checkpoint(),profile=profile.report())
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('checkpoint',?)",(db_dump(value),));db.commit()
                checkpoint_index=reader.rows_scanned;last=monotonic()
                policy.check()
                if progress:progress(dict(event='parse_checkpoint',path=file.relative_path,record_index=checkpoint_index))
            from aiops_v4.data.workers import cancelled,check_cancel
            for position in reader:
                try:row=parser.parse(position)
                except ValueError:
                    profile.rejected+=1
                else:
                    profile.add(row)
                    if flows:flows.add(row)
                    else:db.execute('INSERT INTO observations VALUES (?,?,?,?)',(row['series_key'],row['timestamp'],position.record_index,db_dump(row)))
                if reader.rows_scanned%1000==0 and cancelled():
                    commit();check_cancel()
                if reader.rows_scanned-checkpoint_index>=settings['checkpoint_rows'] or monotonic()-last>=settings['checkpoint_seconds']:commit()
            if flows:flows.flush()
            manifest=reader.manifest();manifest['essential_profile']=profile.report()
            if manifest['rows_scanned']!=profile.accepted+profile.rejected:raise ValueError('scanned/accepted/rejected count mismatch')
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('manifest',?)",(db_dump(manifest),));db.commit();policy.check()
            if progress:progress(dict(event='file_done',path=file.relative_path,rows=reader.rows_scanned))
            return manifest
    finally:db.close()


def aggregate_series(task):
    """Sort a complete native series across all input files in a private worker."""
    merged_path,batch,series,settings,path=task;path=Path(path)
    from aiops_v4.data.workers import check_cancel
    from .aggregate import iter_windows
    policy=ResourcePolicy(settings['workspace_root'],settings['min_free_bytes'],settings['max_workspace_bytes'],settings['memory_bytes'],settings['workers'])
    policy.worker_limit();check_cancel()
    if path.exists():path.unlink()
    source=sqlite3.connect(Path(merged_path).as_uri()+'?mode=ro',uri=True);source.execute('PRAGMA cache_size=-8192')
    target=sqlite3.connect(path);target.execute('CREATE TABLE windows(position INTEGER PRIMARY KEY,payload BLOB)')
    try:
        def rows():
            for count,(payload,) in enumerate(source.execute('SELECT payload FROM observations WHERE series=? ORDER BY time,file,idx',(series,)),1):
                if count%1000==0:check_cancel()
                yield load_json(payload)
        for i,window in enumerate(iter_windows(rows(),batch,settings['window_seconds'],settings['expected_step_seconds'],settings['counter_max_gap_seconds']),1):
            target.execute('INSERT INTO windows VALUES (?,?)',(i,db_dump(window)))
            if i%256==0:check_cancel();policy.check()
        target.commit();policy.check()
        return dict(path=str(path),worker_pid=__import__('os').getpid())
    finally:target.close();source.close()

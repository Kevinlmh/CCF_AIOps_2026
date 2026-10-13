"""Private resumable file spools; NetFlow never writes one sort row per flow."""
from collections import OrderedDict
from contextlib import closing
from dataclasses import replace
from datetime import datetime,timezone
import json
import math
from pathlib import Path
import sqlite3
from time import monotonic

from aiops_v4.data.position import PositionReader, record_id
from aiops_v4.data.essential import EssentialProfile
from aiops_v4.data.discovery import TIME_FIELDS
from aiops_v4.data.reader import is_missing
from aiops_v4.data.resources import ResourcePolicy
from aiops_v4.storage import db_dump,load_json
from .series import series_dimensions,VIEWS
from .identity import entity_identity
from .semantics import applicable_metrics
from .time import observation_time,iso_time,time_zone
from .netflow import NetflowAccumulator
from .spool_codec import ObservationCodec
from .ingest_batch import BoundedSQLBatch, BoundedMemo, ColumnPlan


def _bounded(cache,key,create):
    return cache.get_or_create(key,create)


class PreparedParser:
    """Compile source slots and cache repeated time/grain without changing validation."""
    def __init__(self,reader,naive_timezone,netflow_streaming=True):
        self.reader=reader;self.zone=naive_timezone
        self.netflow_streaming=netflow_streaming
        self.times=BoundedMemo();self.grains=BoundedMemo()
        self.plan=ColumnPlan(reader.file.source,reader.columns,netflow_streaming)
        self.numeric=reader.normalizer._numeric
        self.last_time_key=None;self.last_time_value=None

    def _general(self,position):
        raw=self.plan.fill(position.cells)
        extra=position.cells[len(self.reader.columns):]
        normalizer=self.reader.normalizer
        normalizer.rows_scanned=position.record_index
        identifier=record_id(self.reader.batch,self.reader.file.relative_path,position.record_index,raw,extra)
        grain_key=self.plan.values(position.cells,self.plan.grain)
        normalized_grain=_bounded(self.grains,('normalize',grain_key),lambda:normalizer._normalize_grain(raw))
        record=normalizer._normalize(raw,extra,identifier,position.line_start,position.line_end,normalized_grain)
        time_key=self.plan.values(position.cells,self.plan.time)
        if time_key==self.last_time_key:
            stamp,time_flags=self.last_time_value
        else:
            value,time_flags=observation_time(record,self.zone)
            stamp=iso_time(value)
            # General files often have unique times. Keep only the immediately
            # previous small key; avoid a measured cache miss for every row.
            if all(value is None or len(value)<=1024 for value in time_key):
                self.last_time_key=time_key;self.last_time_value=(stamp,time_flags)
            else:self.last_time_key=None;self.last_time_value=None
        def grain():
            identity=entity_identity(record);dimensions=series_dimensions(record)
            key=json.dumps([self.reader.batch,record.source,identity['entity_id'],dimensions],ensure_ascii=False,sort_keys=True,separators=(',',':'))
            return dict(batch=self.reader.batch,series_key=key,source=record.source,view=VIEWS[record.source],identity=identity,dimensions=dimensions)
        template=_bounded(self.grains,('template',grain_key),grain)
        flags=(set(record.quality_flags)-{'assumed_utc'})|set(time_flags)
        for required in {'interface':('interface_id',),'routing':('metric_name',),
                         'netflow':('interface_id','protocol'),'traffic':('series_key','flow_type'),
                         'scrape':('target_id',)}.get(record.source,()):
            if not template['dimensions'].get(required):flags.add('missing_dimension:'+required)
        ref=dict(record_id=identifier,source_file=self.reader.file.relative_path,
                 record_index=position.record_index,line_start=position.line_start,line_end=position.line_end,
                 byte_start=position.byte_start,byte_length=position.byte_length)
        return dict(template,timestamp=stamp,raw_values=self.plan.metrics(raw),flags=sorted(flags),ref=ref)

    def parse(self,position):
        if self.reader.file.source!='netflow' or not self.netflow_streaming:
            return self._general(position)
        cells=position.cells
        time_key=self.plan.values(cells,self.plan.time)
        def parse_time(values):
            dummy=self.reader.normalizer._normalize(values,(),'',1,1)
            stamp,time_flags=observation_time(dummy,self.zone)
            return iso_time(stamp),sorted((set(dummy.quality_flags)-{'assumed_utc','unmapped_entity','short_csv_row'})|set(time_flags))
        primary=self.plan.time_names[0];primary_value=time_key[0]
        # Preserve the complete fallback validator when primary time is unusable,
        # including timezone ambiguity/nonexistent-time and quality semantics.
        try:
            stamp,time_flags=_bounded(self.times,('primary',primary_value),lambda:parse_time({primary:primary_value}))
        except ValueError:
            stamp,time_flags=_bounded(self.times,('fallback',*time_key),lambda:parse_time(dict(zip(self.plan.time_names,time_key))))
        grain_key=self.plan.values(cells,self.plan.grain)
        def grain():
            meta=dict(zip(self.plan.grain_names,grain_key));meta['minute_utc']='2000-01-01T00:00:00Z'
            record=self.reader.normalizer._normalize(meta,(),'',1,1)
            identity=entity_identity(record);dimensions=series_dimensions(record)
            key=json.dumps([self.reader.batch,'netflow',identity['entity_id'],dimensions],ensure_ascii=False,sort_keys=True,separators=(',',':'))
            flags=set(record.quality_flags)-{'short_csv_row','missing_timestamp'}
            for name in ('interface_id','protocol'):
                if not dimensions.get(name):flags.add('missing_dimension:'+name)
            return dict(batch=self.reader.batch,series_key=key,source='netflow',view=VIEWS['netflow'],identity=identity,dimensions=dimensions),flags
        template,flags=_bounded(self.grains,grain_key,grain)
        flags=set(flags)|set(time_flags)
        if len(cells)<len(self.reader.columns):flags.add('short_csv_row')
        if len(cells)>len(self.reader.columns):flags.add('extra_csv_cells')
        values={}
        for name,index in self.plan.numeric:
            value=self.plan.cell(cells,index)
            if is_missing(value):values[name]=None;continue
            try:number=float(value)
            except (ValueError,TypeError):values[name]=value;flags.add('invalid_numeric:'+name);continue
            if not math.isfinite(number):values[name]=value;flags.add('nonfinite_numeric:'+name)
            else:values[name]=number
        ref=dict(record_id=f'position:{self.reader.file.relative_path}:{position.record_index}',source_file=self.reader.file.relative_path,
                 record_index=position.record_index,line_start=position.line_start,line_end=position.line_end,
                 byte_start=position.byte_start,byte_length=position.byte_length,_cells=cells)
        return dict(template,timestamp=stamp,raw_values=values,flags=sorted(flags),ref=ref)


class FastFlows(NetflowAccumulator):
    def _time_batch(self):
        if not hasattr(self,'time_batch'):
            self.time_batch=BoundedSQLBatch(self.db,'INSERT OR IGNORE INTO flow_times VALUES (?,?,?)')
        return self.time_batch

    def flush(self):
        if hasattr(self,'time_batch'):self.time_batch.flush()
        super().flush()

    def add(self,row):
        key=row['series_key'],self.bucket_start(row['timestamp'])
        value=self._value(key,row)
        new_metrics=[]
        value['count']+=1
        flags_changed=bool(row['flags']) and any(flag not in value['flags'] for flag in row['flags'])
        if flags_changed:value['flags']=sorted(set(value['flags'])|set(row['flags']))
        ref=dict(time=row['timestamp'],ref=row['ref']);order=lambda r:(r['time'],r['ref']['source_file'],r['ref']['record_index'])
        refs_changed=len(value['refs'])<8 or order(ref)<order(value['refs'][-1])
        if refs_changed:value['refs']=sorted(value['refs']+[ref],key=order)[:8]
        if value.get('last_time_seen')!=row['timestamp']:
            self._time_batch().add((*key,row['timestamp']));value['last_time_seen']=row['timestamp']
        for name,raw in row['raw_values'].items():
            m=value['metrics'].get(name)
            if m is None:
                new_metrics.append(name)
                m=value['metrics'][name]=dict(inputs=0,missing=0,invalid=0,n=0,zeros=0,mean=0.,m2=0.,total=0.,min=None,max=None,first=None,last=None)
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
        self._remember_value(key,value,refs_changed,flags_changed,new_metrics)


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
        fresh = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'").fetchone() is None
        db.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,payload BLOB)')
        db.execute('CREATE TABLE IF NOT EXISTS observations(series INTEGER,time TEXT,idx INTEGER,payload BLOB)')
        codec = ObservationCodec(db, create=fresh)
        observations=BoundedSQLBatch(db,'INSERT INTO observations VALUES (?,?,?,?)')
        saved=db.execute("SELECT payload FROM metadata WHERE key='checkpoint'").fetchone();saved=load_json(saved[0]) if saved else None
        done=db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()
        if done:
            # Completed legacy spools may predate the private ordering index.
            with policy.guard_sql(db):
                db.execute('CREATE INDEX IF NOT EXISTS observations_order ON observations(series,time,idx)')
            db.commit()
            if progress:progress(dict(event='file_reused',path=file.relative_path))
            return load_json(done[0])
        profile=EssentialProfile(saved['profile'] if saved else None)
        flows=None
        if file.source=='netflow' and settings['netflow_streaming']:
            flow_cache_budget=max(1,settings['memory_bytes']//(8*settings['workers']))
            flow_cache_capacity=max(1,min(65536,flow_cache_budget//8192))
            if not saved:
                # DDL may survive an interruption before the first transaction
                # checkpoint. With no committed position, discard only this
                # file's private, uncommitted accumulator and start at byte 0.
                db.execute('DELETE FROM observations')
                db.execute('DROP TABLE IF EXISTS flow_windows');db.execute('DROP TABLE IF EXISTS flow_times')
                flows=FastFlows(db,settings['window_seconds'],settings['expected_step_seconds'],capacity=flow_cache_capacity,cache_budget_bytes=flow_cache_budget)
            else:
                flows=FastFlows.__new__(FastFlows);flows.db=db;flows.width=settings['window_seconds'];flows.step=settings['expected_step_seconds'];flows.capacity=flow_cache_capacity;flows.cache=OrderedDict()
                flows.configure_cache(flow_cache_capacity,flow_cache_budget)
        with PositionReader(file,batch,settings['max_rows_per_file'],saved['reader'] if saved else None) as reader:
            parser=PreparedParser(reader,settings['naive_timezone'],settings['netflow_streaming']);last=monotonic();checkpoint_index=reader.rows_scanned
            if progress and saved:progress(dict(event='parse_resumed',path=file.relative_path,record_index=reader.rows_scanned))
            def commit():
                nonlocal last,checkpoint_index
                observations.flush()
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
                    else:
                        series, payload = codec.encode(row)
                        observations.add((series,row['timestamp'],position.record_index,payload))
                if reader.rows_scanned%1000==0 and cancelled():
                    commit();check_cancel()
                if reader.rows_scanned-checkpoint_index>=settings['checkpoint_rows'] or monotonic()-last>=settings['checkpoint_seconds']:commit()
            observations.flush()
            if flows:flows.flush()
            with policy.guard_sql(db):
                db.execute('CREATE INDEX IF NOT EXISTS observations_order ON observations(series,time,idx)')
            manifest=reader.manifest();manifest['essential_profile']=profile.report()
            if flows:manifest['netflow_cache']=flows.cache_report()
            if manifest['rows_scanned']!=profile.accepted+profile.rejected:raise ValueError('scanned/accepted/rejected count mismatch')
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('manifest',?)",(db_dump(manifest),));db.commit();policy.check()
            if progress:progress(dict(event='file_done',path=file.relative_path,rows=reader.rows_scanned))
            return manifest
    finally:db.close()


def aggregate_series(task):
    """Aggregate one native series from private files or a legacy merged spool."""
    merged_path,batch,series,settings,path=task;path=Path(path)
    from aiops_v4.data.workers import check_cancel
    from .aggregate import iter_windows
    policy=ResourcePolicy(settings['workspace_root'],settings['min_free_bytes'],settings['max_workspace_bytes'],settings['memory_bytes'],settings['workers'])
    policy.worker_limit();check_cancel()
    if path.exists():path.unlink()
    private_sources = isinstance(merged_path, (list, tuple))
    source = None if private_sources else sqlite3.connect(Path(merged_path).as_uri()+'?mode=ro',uri=True)
    if source is not None:source.execute('PRAGMA cache_size=-8192')
    target=sqlite3.connect(path)
    from .write_codec import PreparedWindowWriter,canonicalize_window
    try:
        output=PreparedWindowWriter(target,batch)
        manifests={}
        if private_sources:
            for _,source_path in merged_path:
                manifest_db=sqlite3.connect(Path(source_path).as_uri()+'?mode=ro',uri=True)
                try:
                    if manifest_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone():
                        saved=manifest_db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()
                        if saved:
                            manifest=load_json(saved[0]);manifests[manifest['path']]=manifest
                finally:manifest_db.close()
        def rows():
            if private_sources:
                from .source_stream import iter_prepared_rows
                records = iter_prepared_rows(merged_path, series)
            else:
                records = (load_json(payload) for payload, in source.execute(
                    'SELECT payload FROM observations WHERE series=? ORDER BY time,file,idx',(series,)))
            # A retained cancellation traceback can keep generator frames alive.
            # Close the private source ExitStack immediately on every exit.
            with closing(records):
                for count,row in enumerate(records,1):
                    if count%1000==0:check_cancel()
                    yield row
        with closing(rows()) as records:
            with closing(iter_windows(records,batch,settings['window_seconds'],settings['expected_step_seconds'],settings['counter_max_gap_seconds'])) as windows:
                for i,window in enumerate(windows,1):
                    output.add(*canonicalize_window(window,manifests,batch))
                    if i%256==0:check_cancel();policy.check()
        output.flush()
        target.commit();policy.check()
        return dict(path=str(path),worker_pid=__import__('os').getpid())
    finally:
        target.close()
        if source is not None:source.close()

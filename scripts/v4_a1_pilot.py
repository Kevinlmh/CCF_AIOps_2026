"""A1 pilot only: derive a small sealed evidence index; never modify upstream stores."""
import argparse,collections,copy,gzip,hashlib,json,shutil,sqlite3,sys,time
from datetime import datetime,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
MAP={('dns','beida','dns.beida.aiops.local'):('beida-service-vm-1','fd00:1:40::31','udp',1053),
 ('web','shanghai','web01.shanghai.aiops.local'):('shanghai-service-vm-1','fd00:6:40::31','tcp',80),
 ('auth','wuhan','auth01.wuhan.aiops.local'):('wuhan-service-vm-1','fd00:5:40::31','tcp',8080)}
def stamp(s):return datetime.fromisoformat(s.replace('Z','+00:00'))
def hashobj(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def endpoint_hint(key):
 if tuple(key) not in MAP:return None
 n,a,p,port=MAP[tuple(key)]
 return dict(network_element_id=n,destination_address=a,transport=p,destination_port=port,
  flow_type=key[0],target_region=key[1],target_domain=key[2],causal_root_confirmed=False,
  provenance='User-provided Opus scrape_health/NetFlow cross-check; address association is not causal diagnosis')
def targets(b):
 return {tuple(d.get(k) for k in ('flow_type','target_region','target_domain')) for s in b.get('support',[]) for d in [s.get('dimensions',{})] if d.get('target_domain') and d.get('target_region')}
def cluster(packets,gap_seconds=120):
 grouped=collections.defaultdict(list);out=[]
 for b in packets:
  keys=targets(b)
  if len(keys)!=1:out.append(dict(keys=sorted(keys,key=str),packets=[b],side='ambiguous'));continue
  grouped[next(iter(keys))].append(b)
 for key,rows in grouped.items():
  cur=None
  for b in sorted(rows,key=lambda b:(stamp(b['start_time']),b['bundle_id'])):
   if cur is None or stamp(b['start_time'])>stamp(cur['end'])+timedelta(seconds=gap_seconds):
    cur=dict(keys=[key],packets=[],end=b['end_time']);out.append(cur)
   cur['packets'].append(b);cur['end']=max(cur['end'],b['end_time'],key=stamp)
  for g in out:
   if g.get('keys')==[key] and 'side' not in g:g['side']='target' if len({x['entity_id'].split('-')[0] for x in g['packets']})>=2 else 'single_pair'
 for g in out:
  g['id']=hashobj(dict(policy='homogeneous_target_no_multitarget_bridge',gap_seconds=gap_seconds,members=sorted(b['bundle_id'] for b in g['packets'])))
 return sorted(out,key=lambda g:(min(stamp(b['start_time']) for b in g['packets']),g['id']))
def choose_even(rows,n):
 rows=sorted(rows,key=lambda x:(min(b['start_time'] for b in x['packets']),x['id']))
 if len(rows)<=n:return rows
 return [rows[i*(len(rows)-1)//(n-1)] for i in range(n)] if n>1 else [rows[0]]

def select_observations(rows,limit=32,priority_entities=()):
 """Keep target/peer node context, then round-robin participant/source strata."""
 unique={}
 for row in rows:unique.setdefault(row['vector_id'],row)
 rows=list(unique.values());priority=set(priority_entities)
 retained=[r for r in rows if r.get('source')=='node' and r.get('entity_id') in priority]
 if len(retained)>limit:raise ValueError('target context exceeds observation cap; split work unit')
 used={r['vector_id'] for r in retained};groups=collections.defaultdict(list)
 for r in rows:
  if r['vector_id'] not in used:
   dims=r.get('dimensions') or {}
   key=(r.get('source',''),r.get('entity_id',''),str(dims.get('flow_type','')),str(dims.get('target_domain','')))
   groups[key].append(r)
 keys=sorted(groups);offset=0
 while len(retained)<limit:
  progress=False
  for key in keys:
   if offset<len(groups[key]):
    retained.append(groups[key][offset]);progress=True
    if len(retained)>=limit:break
  if not progress:break
  offset+=1
 return retained

def build(args):
 from aiops_v4.evidence.index import read_index,metadata,query_evidence
 from aiops_v4.agents.contracts import load_manifest
 from aiops_v4.agents.tools import compact_role_observation
 from aiops_v4.storage import load_json,db_dump
 from aiops_v4.data.cache import verify_layer,seal_layer,sha256
 source=Path(args.database).resolve(strict=True);out=Path(args.output).resolve();report=Path(args.report).resolve()
 report.mkdir(parents=True,exist_ok=True)
 if out.exists():raise FileExistsError('pilot index already exists; no overwrite')
 # Sibling keeps the exact immutable state/feature dependency links, never copies them.
 if out.parent!=source.parent.parent:raise ValueError('pilot must be a sibling of the original sealed evidence layer')
 before=sha256(source);sealed=verify_layer(source.parent)
 packets=[json.loads(l) for l in gzip.open(source.parent/'bundles.jsonl.gz','rt') if l.strip()]
 by_id={b['bundle_id']:b for b in packets}
 traffic=[b for b in packets if b['entity_id'].endswith('-traffic-vm')];other=[b for b in packets if not b['entity_id'].endswith('-traffic-vm')]
 groups=cluster(traffic,args.gap_seconds);sensitivity=cluster(traffic,120)
 sizes={};manifests={}
 print(json.dumps(dict(event='loaded_packets',total=len(packets),traffic=len(traffic),units=len(groups),gap_seconds=args.gap_seconds)),flush=True)
 # One native source connection; verify sealed dependencies once per process.
 with read_index(source,'stage1') as original:
  def events_for(bids):
   return sorted({r[0] for bid in bids for r in original.execute('SELECT event FROM members WHERE bundle=?',(bid,))})
  def windows(b):
   if b['bundle_id'] not in manifests:manifests[b['bundle_id']]=load_manifest(source,'stage1',b['bundle_id'],2000)
   return manifests[b['bundle_id']]
  eligible={flow:[] for flow in ['dns','web','auth']};excluded=collections.Counter()
  for g in groups:
   if g['side']!='target' or len(g['keys'])!=1 or not endpoint_hint(g['keys'][0]):continue
   manifest=[w for b in g['packets'] for w in windows(b)]
   if len(manifest)>args.max_windows:excluded['too_many_candidate_windows']+=1;continue
   eligible[g['keys'][0][0]].append(g)
  selected=[]
  for flow in eligible:selected.extend(choose_even(eligible[flow],6))
  resource=[b for b in other if b['entity_id']=='beida-service-vm-1']
  dns_intervals=[(min(stamp(b['start_time']) for b in g['packets']),max(stamp(b['end_time']) for b in g['packets'])) for g in groups if g['keys'] and g['keys'][0][0]=='dns']
  for overlap,n in [(False,4),(True,4)]:
   choices=[]
   for b in resource:
    flag=any(stamp(b['start_time'])<e and stamp(b['end_time'])>s for s,e in dns_intervals)
    if flag==overlap:choices.append(dict(id=b['bundle_id'],packets=[b],keys=[],side='resource_overlap' if overlap else 'resource_independent'))
   selected.extend(choose_even(choices,n))
  if len(selected)<20:raise ValueError('insufficient eligible pilot scope; inspect selection rather than silently changing policy')
  out.mkdir();target=out/'evidence.sqlite';shutil.copy2(source,target);target.chmod(0o600)
  db=sqlite3.connect(target)
  try:
   db.execute('PRAGMA foreign_keys=OFF');db.executescript('DELETE FROM bundles;DELETE FROM members;DELETE FROM relations;')
   selection=[];all_events=set();all_windows=set()
   for number,g in enumerate(selected):
    bids=sorted(b['bundle_id'] for b in g['packets']);native=events_for(bids)
    if all_events.intersection(native):raise ValueError('pilot events assigned to more than one work unit')
    all_events.update(native);manifest=sorted([w for b in g['packets'] for w in windows(b)],key=lambda w:(w['start_time'],w['end_time'],w['vector_id']))
    ids=[w['vector_id'] for w in manifest]
    if len(ids)!=len(set(ids)) or all_windows.intersection(ids):raise ValueError('candidate window duplicated across work units')
    all_windows.update(ids);start=min(b['start_time'] for b in g['packets']);end=max(b['end_time'] for b in g['packets'])
    packet=copy.deepcopy(g['packets'][0]);bid=hashobj(dict(pilot=g['id'],members=bids))
    packet.update(bundle_id=bid,start_time=start,end_time=end,event_ids=native,event_count=len(native),event_ids_truncated=False,
      association='experimental_same_target_symptoms_not_causal_proof',relations=[],relation_count=0,
      experimental_incident=dict(version=1,kind=g['side'],source_bundle_ids=bids,candidate_window_count=len(ids),gap_seconds=args.gap_seconds,
       preserve_original_candidate_windows=True,selection_scope='26_unit_pilot_not_full_stage1',root_hint_is_not_diagnosis=True))
    hint=endpoint_hint(g['keys'][0]) if len(g['keys'])==1 else None
    if hint:packet['service_endpoint_context']=hint
    # Balanced existing observations keep all participant cities represented.
    support=[];counter=[];quality=[]
    for b in g['packets']:
     for key,dst,cap in [('support',support,1),('observations_without_current_trigger',counter,2),('quality_context',quality,1)]:
      rows=b.get(key,[])
      if rows:dst.extend([rows[0]] if cap==1 or len(rows)==1 else [rows[0],rows[-1]])
    extra_count=0
    if hint:
     city=hint['target_region'];lo=(stamp(start)-timedelta(minutes=2)).isoformat();hi=(stamp(end)+timedelta(minutes=2)).isoformat()
     for entity in [hint['network_element_id'],city+'-service-vm-2',city+'-service-vm-3']:
      found=query_evidence(source,'stage1','states',entity_id=entity,source='node',start=lo,end=hi,limit=1000)
      if not found:continue
      def value(r,k):return r.get('matrix',{}).get('features',{}).get(k,{}).get('raw_value',0) or 0
      chosen=[found[0],max(found,key=lambda r:value(r,'cpu_usage::mean')),max(found,key=lambda r:value(r,'disk_io_util::mean')),found[-1]]
      seen=set()
      for row in chosen:
       if row['vector_id'] in seen:continue
       seen.add(row['vector_id']);v=compact_role_observation(row,row['matrix']);extra_count+=1
       if v.get('scoring_status')!='scored':quality.append(v)
       elif v.get('candidate'):support.append(v)
       else:counter.append(v)
    def unique(rows):
     d={}
     for row in rows:d.setdefault(row['vector_id'],row)
     return list(d.values())
    lists={'support':unique(support),'observations_without_current_trigger':unique(counter),'quality_context':unique(quality)}
    packet['observation_counts']={k:len(v) for k,v in lists.items()};packet['truncation']={k:len(v)>32 for k,v in lists.items()}
    priority={hint['network_element_id'],hint['target_region']+'-service-vm-2',hint['target_region']+'-service-vm-3'} if hint else set()
    packet.update({k:select_observations(v,32,priority) for k,v in lists.items()})
    packet['observation_sampling']='target/peer node priority then round-robin source/entity/flow strata'
    ctx=dict(packet.get('context',{}));ctx.pop('local_availability',None);ctx.update(merged_scope='participant original packets plus bounded target/peer node observations',
     source_bundle_count=len(bids),target_node_observations_added=extra_count,not_known_healthy=True)
    packet['context']=ctx
    packet['limitations']=list(dict.fromkeys(packet.get('limitations',[])+['experimental_cross_city_symptom_association','target_mapping_does_not_establish_cause','bounded_observation_samples_not_exhaustive','resource_overlap_does_not_establish_same_fault']))
    # Top-level raw anchors stay with the representative packet; state-specific provenance remains intact.
    db.execute('INSERT INTO bundles VALUES (?,?,?,?,?)',(bid,packet['entity_id'],start,end,db_dump(packet)))
    db.executemany('INSERT INTO members VALUES (?,?)',[(e,bid) for e in native])
    selection.append(dict(id=bid,kind=g['side'],targets=g['keys'],source_bundle_ids=bids,source_cities=sorted({b['entity_id'].split('-')[0] for b in g['packets']}),
      candidate_windows=len(ids),native_events=len(native),target_node_observations=extra_count,start=start,end=end))
    print(json.dumps(dict(event='pilot_packet',number=number+1,total=len(selected),windows=len(ids),kind=g['side'])),flush=True)
   db.commit();db.close();db=None
   if sha256(source)!=before:raise ValueError('upstream changed during pilot derivation')
   manifest=dict(schema_version=1,created_at=time.time(),scope='A1 pilot only, not full first batch',source_database=str(source),source_sha256=before,
    source_code_sha256=sha256(__file__),groups_gap0_or_selected=len(groups),gap_seconds=args.gap_seconds,groups_gap120=len(sensitivity),
    multi_target_policy='retain separate original bundle; never bridge distinct services',selection_policy='6 evenly spaced per dns/web/auth among <=120 window target incidents, 4 independent and 4 overlapping resource controls',
    eligible_by_flow={k:len(v) for k,v in eligible.items()},excluded=dict(excluded),units=selection,unit_count=len(selection),preserved_native_events=len(all_events),preserved_candidate_windows=len(all_windows))
   (out/'pilot_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
   summary={k:v for k,v in sealed.items() if k not in ['artifact_sha256','dependencies','layer_signature']}
   summary.update(output_dir=str(out),bundles=len(selection),experimental=True,experimental_scope='a1_pilot_only',pilot_manifest_sha256=sha256(out/'pilot_manifest.json'),source_evidence_sha256=before)
   deps=dict(sealed['dependencies']);deps['../'+source.parent.name+'/evidence.sqlite']=before
   result=seal_layer(out,summary,signature=hashobj(manifest),dependencies=deps)
   (report/'selection.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2));(report/'build_complete.json').write_text(json.dumps(dict(database=str(target),database_sha256=sha256(target),summary_sha256=sha256(out/'summary.json'),sealed=True,units=len(selection),elapsed_finished_epoch=time.time()),indent=2))
   print(json.dumps(dict(event='pilot_build_complete',units=len(selection),database=str(target),size_bytes=target.stat().st_size)),flush=True)
  finally:
   if db:db.close()
def main():
 p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--output',required=True);p.add_argument('--report',required=True);p.add_argument('--gap-seconds',type=int,default=0);p.add_argument('--max-windows',type=int,default=120);a=p.parse_args();build(a)
if __name__=='__main__':main()

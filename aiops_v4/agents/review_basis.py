"""Observed causal bases, not labels: prevent unsupported normal-node RCA acceptance."""
import json
from pathlib import Path
from functools import lru_cache

@lru_cache(maxsize=1)
def knowledge():return json.loads(Path(__file__).with_name("endpoint_knowledge.json").read_text())

def endpoint_associations(rows):
    actual={(r.get("dimensions",{}).get("flow_type"),str(r.get("dimensions",{}).get("target_domain","")).lower().rstrip("."),r.get("dimensions",{}).get("target_region")) for r in rows if r.get("source")=="traffic"}
    config=knowledge()
    return [dict(t,provenance=config["provenance"],meaning=config["meaning"]) for t in config["targets"] if (t["flow_type"],t["target_domain"],t["target_region"]) in actual]

def first_root(payload):
    given=payload.get("independent_diagnoses") or payload.get("diagnoses") or {}
    rows=((given.get("localization") or {}).get("result") or {}).get("candidates") or []
    return rows[0].get("network_element_id") if rows else None

def causal_bases(payload,seen):
    from .tools import entity_of
    from .structured import available_link_subcategories
    from aiops_challenge_2026.schema import parse_utc
    root=first_root(payload);event=payload.get("event") or {};ids=set(event.get("window_ids",[]));bases=set()
    if not root:return bases
    def overlaps(row):
        try:return parse_utc(row["start_time"])<parse_utc(event["end_time"]) and parse_utc(row["end_time"])>parse_utc(event["start_time"])
        except (KeyError,ValueError,TypeError):return False
    raw_ids={r["record_id"] for (k,i),row in seen.items() if k=="state" and i in ids for r in row.get("references",[])}
    for key,row in seen.items():
        if entity_of(row)!=root:continue
        kind,identifier=key
        if kind=="raw" and identifier in raw_ids:bases.add("event_raw_observation")
        if kind!="state" or not overlaps(row):continue
        source=row.get("source")
        if row.get("candidate") is True:
            if source=="node":bases.add("local_resource_change")
            if source=="routing":bases.add("routing_state_change")
            if source in ("frr","log"):bases.add("log_state_change")
        if source!="traffic" and available_link_subcategories({key:row}):bases.add("link_measurement")
    observed_targets={(row.get("dimensions",{}).get("flow_type"),str(row.get("dimensions",{}).get("target_domain","")).lower().rstrip("."),row.get("dimensions",{}).get("target_region")) for (k,i),row in seen.items() if k=="state" and i in ids and row.get("source")=="traffic"}
    for a in (payload.get("bundle") or {}).get("endpoint_associations",[]):
        if a.get("mapped_service_node")==root and (a.get("flow_type"),a.get("target_domain"),a.get("target_region")) in observed_targets:bases.add("mapped_service_target")
    return bases

def basis_witnesses(payload,seen):
    from .tools import entity_of
    root=first_root(payload);ids=set((payload.get("event") or {}).get("window_ids",[]));result={}
    traffic={k:r for k,r in seen.items() if k[0]=="state" and k[1] in ids and r.get("source")=="traffic"}
    event_states={k:r for k,r in seen.items() if k[0]=="state" and k[1] in ids}
    for key,row in seen.items():
        if key[0] not in ("state","raw") or entity_of(row)!=root:continue
        subset=dict(traffic);subset[key]=row
        if key[0]=="raw":subset.update(event_states)
        bases=causal_bases(payload,subset)
        if key[0]=="raw":bases&={"event_raw_observation"}
        for basis in bases:result.setdefault(basis,set()).add(key)
    return result

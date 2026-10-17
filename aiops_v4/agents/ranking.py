"""Bounded actual observations for ranked alternatives; no synthetic root filling."""
from functools import lru_cache
import json

@lru_cache(maxsize=128)
def _context(database,batch,start,end,observed,entity):
    from aiops_challenge_2026.schema import VALID_NETWORK_ELEMENTS
    from aiops_v4.evidence.index import query_evidence
    from .tools import compact_initial_observation
    cities=({entity.split("-",1)[0]} if entity in VALID_NETWORK_ELEMENTS else {n.split("-",1)[0] for n in observed if n in VALID_NETWORK_ELEMENTS})
    primary=([entity] if entity in VALID_NETWORK_ELEMENTS else [])+[n for n in observed if n!=entity and not n.endswith("-traffic-vm")]
    nodes=primary+sorted(n for n in VALID_NETWORK_ELEMENTS if n.split("-",1)[0] in cities and n not in observed and not n.endswith("-traffic-vm"))
    result=[]
    for node in dict.fromkeys(nodes):
        if len(result)>=8:break
        values=query_evidence(database,batch,"states",entity_id=node,source="node",start=start,end=end,limit=1)
        if values:
            row=values[0];result.append(compact_initial_observation(row,row.get("matrix"),profile="a1-readable-1"))
    return json.dumps(result,ensure_ascii=False,allow_nan=False)

def root_context_observations(database,batch,start,end,observed,entity):
    # Return fresh objects: prepare_candidates removes repeated unit annotations.
    return json.loads(_context(database,batch,start,end,observed,entity))

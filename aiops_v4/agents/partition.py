"""Deterministic observed-time components; retain every candidate window exactly once."""
from aiops_challenge_2026.schema import parse_utc

def observed_components(manifest):
    aliases={w["vector_id"]:"w"+str(i) for i,w in enumerate(manifest)}
    if len(aliases)!=len(manifest):raise ValueError("duplicate manifest window")
    groups=[];cursor=None
    for row in sorted(manifest,key=lambda r:(parse_utc(r["start_time"]),parse_utc(r["end_time"]),r["vector_id"])):
        start,end=parse_utc(row["start_time"]),parse_utc(row["end_time"])
        if start>=end:raise ValueError("invalid candidate interval")
        if cursor is None or start>cursor:
            groups.append(dict(key="c"+str(len(groups)),window_ids=[],window_aliases=[],start_time=row["start_time"],end_time=row["end_time"]));cursor=end
        group=groups[-1];group["window_ids"].append(row["vector_id"]);group["window_aliases"].append(aliases[row["vector_id"]])
        if end>cursor:group["end_time"]=row["end_time"];cursor=end
    return groups

def component_description(manifest):
    return {g["key"]:{k:g[k] for k in ("window_aliases","start_time","end_time")} for g in observed_components(manifest)}

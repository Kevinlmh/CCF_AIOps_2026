"""Reproducibility fingerprints and explicit, coverage-aware price estimates."""
import hashlib
from pathlib import Path
import platform
import subprocess
import sys
from aiops_v4.agents.jsonio import dumps
from aiops_v4.agents.prompts import ROLE_TEXT, system_prompt, PROMPT_VERSION
from .knowledge import knowledge_cards


def hash_object(value):
    return hashlib.sha256(dumps(value).encode('utf-8')).hexdigest()


def provenance(config):
    root=Path(__file__).resolve().parents[2]
    files=[]
    for package in ('aiops_v4','aiops_common','aiops_challenge_2026'):
        files.extend(p for p in (root/package).rglob('*') if p.is_file() and p.suffix in ('.py','.json'))
    source=hashlib.sha256()
    for p in sorted(files):
        source.update(str(p.relative_to(root)).encode()); source.update(b'\0'); source.update(p.read_bytes()); source.update(b'\0')
    def git(*args):
        try:
            r=subprocess.run(['git',*args],cwd=root,capture_output=True,text=True,timeout=3)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError,subprocess.TimeoutExpired): return None
    return dict(config_sha256=hash_object(config),code_sha256=source.hexdigest(),
                prompt_version=PROMPT_VERSION,prompt_sha256={r:hash_object(system_prompt(r)) for r in sorted(ROLE_TEXT)},
                knowledge_sha256=hash_object(knowledge_cards()),python=sys.version,platform=platform.platform(),
                git_commit=git('rev-parse','HEAD'),git_dirty=bool(git('status','--porcelain')),
                algorithm_randomness='deterministic ordered bounded reference and k-medoids; no random seed configured')


def estimate_cost(summary, pricing):
    result=dict(currency='USD',estimated_cost=None,known_usage_cost=None,pricing=pricing,
                backend_calls=summary.get('backend_calls',0),usage_reported_calls=summary.get('usage_reported_calls',0))
    usage=summary.get('usage',{})
    if summary.get('simulated'):
        result['reason']='simulated_not_billable'
    elif pricing is None:
        result['reason']='prices_not_configured'
    elif not {'prompt_tokens','completion_tokens'} <= usage.keys():
        result['reason']='input_output_usage_missing'
    else:
        result['known_usage_cost']=(usage['prompt_tokens']*pricing['input_per_million']+usage['completion_tokens']*pricing['output_per_million'])/1000000
        complete=(summary.get('usage_complete') and summary.get('backend_calls') == summary.get('usage_reported_calls'))
        result['reason']='configured_rate_estimate' if complete else 'usage_incomplete'
        if complete: result['estimated_cost']=result['known_usage_cost']
    result['billing_note']='configured token-rate estimate only; discounts, cache pricing and provider billing not verified'
    return result

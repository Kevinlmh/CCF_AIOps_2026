"""Strict, label-free experiment configuration; secrets stay in environment."""
from dataclasses import asdict
from pathlib import Path
import re
from aiops_v4.agents.backend import HTTPBackend
from aiops_v4.agents.engine import Budget
from aiops_v4.agents.jsonio import loads
from aiops_v4.features.time import time_zone
from aiops_v4.data.reader import utc_time

DEFAULTS = dict(
    profile=dict(max_rows_per_file=None, reservoir_size=512, max_groups=2048, cardinality_limit=128),
    features=dict(window_seconds=60, expected_step_seconds=None, counter_max_gap_seconds=180),
    states=dict(reference_start=None, reference_end=None, min_reference=12, clusters=3, mode='hybrid',
                stat_threshold=6.0, cluster_threshold=3.0, rare_fraction=0.1, minimum_overlap=0.5, rules_enabled=True),
    evidence=dict(context_seconds=120, observations_per_kind=8, skip_raw=False, topology=None),
    agents=dict(mode='independent', review_mode='llm', knowledge=True, state_explanation=False,
                discovery_only=False, bundle_id=None, limit_bundles=None, budget=asdict(Budget())))


def _object(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError('invalid or unknown configuration fields')
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('nonempty configuration text required')
    return value


def _integer(value, low=1, high=None, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not int or value < low or (high is not None and value > high):
        raise ValueError('integer configuration out of range')


def _number(value, low, high, exclusive_low=False):
    # Bounded comparison before float conversion also rejects huge Python integers.
    if type(value) not in {int, float} or not (low < value <= high if exclusive_low else low <= value <= high):
        raise ValueError('numeric configuration out of range')


def _bool(value):
    if type(value) is not bool:
        raise ValueError('boolean configuration required')


def _path(value, base):
    p=Path(_text(value)).expanduser()
    return str((p if p.is_absolute() else base / p).resolve())


def validate_config(config, base=None):
    base=Path(base or Path.cwd()).resolve()
    _object(config, ('schema_version','batch','raw_root','naive_timezone',*DEFAULTS,'backend','pricing'))
    if type(config.get('schema_version')) is not int or config['schema_version'] != 1:
        raise ValueError('configuration schema_version must be 1')
    result={key:_text(config.get(key)) for key in ('batch','raw_root','naive_timezone')}
    result.update(schema_version=1, raw_root=_path(result['raw_root'],base))
    time_zone(result['naive_timezone'])
    for name, defaults in DEFAULTS.items():
        supplied=_object(config.get(name,{}),defaults)
        result[name]={**defaults,**supplied}
    p=result['profile']
    _integer(p['max_rows_per_file'],nullable=True)
    for key in ('reservoir_size','max_groups','cardinality_limit'): _integer(p[key],high=1000000)
    f=result['features']
    for key in ('window_seconds','counter_max_gap_seconds'): _integer(f[key])
    _integer(f['expected_step_seconds'],nullable=True)
    if f['expected_step_seconds'] and f['window_seconds'] % f['expected_step_seconds']:
        raise ValueError('expected step must divide window width')
    s=result['states']
    _integer(s['min_reference'],2,256); _integer(s['clusters'],1,8)
    if s['mode'] not in ('statistics','cluster','hybrid'): raise ValueError('invalid state mode')
    for key in ('stat_threshold','cluster_threshold'): _number(s[key],0,1e9,True)
    _number(s['minimum_overlap'],0,1,True); _number(s['rare_fraction'],0,1); _bool(s['rules_enabled'])
    if (s['reference_start'] is None) != (s['reference_end'] is None): raise ValueError('reference requires both bounds')
    if s['reference_start'] is not None:
        for key in ('reference_start','reference_end'):
            s[key]=utc_time(_text(s[key]),require_timezone=True)[0]
        if s['reference_start'] >= s['reference_end']: raise ValueError('reference must be increasing')
    e=result['evidence']
    _integer(e['context_seconds'],0,86400); _integer(e['observations_per_kind'],1,32); _bool(e['skip_raw'])
    if e['topology'] is not None: e['topology']=_path(e['topology'],base)
    a=result['agents']
    if a['mode'] not in ('independent','joint') or a['review_mode'] not in ('llm','program_only'):
        raise ValueError('invalid agent experiment mode')
    for key in ('knowledge','state_explanation','discovery_only'): _bool(a[key])
    _integer(a['limit_bundles'],nullable=True)
    if a['bundle_id'] is not None: _text(a['bundle_id'])
    if a['limit_bundles'] is not None and a['bundle_id'] is not None: raise ValueError('choose one bundle selection')
    budget={**asdict(Budget()),**_object(a['budget'],asdict(Budget()))}
    a['budget']=asdict(Budget(**budget))
    b=dict(_object(config.get('backend'),('kind','model','replay_file','base_url','api_key_env','timeout','max_response_bytes','max_completion_tokens')))
    _text(b.get('model'))
    if b.get('kind') == 'replay':
        _object(b,('kind','model','replay_file'))
        b['replay_file']=_path(b.get('replay_file'),base)
    elif b.get('kind') == 'http':
        _object(b,('kind','model','base_url','api_key_env','timeout','max_response_bytes','max_completion_tokens'))
        b.setdefault('api_key_env','AIOPS_LLM_API_KEY')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',_text(b['api_key_env'])): raise ValueError('invalid key environment name')
        b.setdefault('timeout',60); b.setdefault('max_response_bytes',1048576); b.setdefault('max_completion_tokens',4096)
        # Constructor validates URL and limits, without sending a request or reading secrets.
        HTTPBackend(b['model'],_text(b.get('base_url')),'preflight-placeholder',
                    b['timeout'],b['max_response_bytes'],b['max_completion_tokens'])
    else:
        raise ValueError('backend kind must be replay or http')
    result['backend']=b
    pricing=config.get('pricing')
    if pricing is not None:
        _object(pricing,('currency','input_per_million','output_per_million'))
        if pricing.get('currency') != 'USD': raise ValueError('explicit USD pricing required')
        for key in ('input_per_million','output_per_million'): _number(pricing.get(key),0,1e9)
        pricing=dict(pricing)
    result['pricing']=pricing
    return result


def load_config(path):
    path=Path(path).resolve(strict=True)
    return validate_config(loads(path.read_text(encoding='utf-8')),path.parent)

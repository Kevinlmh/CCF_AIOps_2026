"""Strict JSON at all model and replay boundaries."""
import json
import math


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('nonfinite JSON number')
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)
    return value


def loads(text):
    return _finite(json.loads(text, object_pairs_hook=_pairs))


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))

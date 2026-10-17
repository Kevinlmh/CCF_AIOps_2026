"""Consumer-visible guidance regressions; no model or HTTP calls."""
import copy

import pytest

from aiops_v4.agents.prompts import PROMPT_VERSION, system_prompt
from aiops_v4.agents.tools import tool_definitions


@pytest.mark.parametrize('compact', [False, True])
def test_state_query_description_explains_event_time_scope(compact):
    query = next(tool['function'] for tool in tool_definitions(compact=compact)
                 if tool['function']['name'] == 'query_states')
    description = query['description']
    assert 'entity/time' in description
    assert '[start,end)' in description and 'overlap' in description
    assert 'earliest' in description and 'batch' in description
    assert 'offset' in description and 'not relative to the event' in description
    for name in ('start', 'end'):
        field = query['parameters']['properties'][name]
        assert field['type'] == 'string'
        assert 'timezone' in field['description'] and '[start,end)' in field['description']


@pytest.mark.parametrize('compact', [False, True])
def test_guidance_preserves_optional_query_filters(compact):
    query = next(tool['function'] for tool in tool_definitions(compact=compact)
                 if tool['function']['name'] == 'query_states')
    parameters = copy.deepcopy(query['parameters'])
    for field in parameters['properties'].values():
        field.pop('description', None)
    assert parameters == {
        'type': 'object',
        'properties': {
            'entity_id': {'type': 'string'}, 'source': {'type': 'string'},
            'group_id': {'type': 'string'}, 'start': {'type': 'string'}, 'end': {'type': 'string'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1 if compact else 1000},
            'offset': {'type': 'integer', 'minimum': 0},
        },
        'required': [], 'additionalProperties': False,
    }
    assert query.get('strict', False) is compact


@pytest.mark.parametrize('role', ['confirmation', 'localization', 'classification', 'review'])
def test_all_diagnostic_prompts_preserve_traffic_and_reference_meaning(role):
    prompt = system_prompt(role)
    for term in ('known_healthy=false', '同批参考', '有效增量', 'batch_duration', '单请求延迟',
                 'timeout/error', 'active_seconds_total', 'seconds/second', 'down', 'UDP',
                 '物理丢包', '接口/路径', 'start/end', '结束后', '仍触发'):
        assert term in prompt
    assert PROMPT_VERSION == 'v4-roles-12-observed-components'


def test_review_requires_its_own_numeric_and_counterevidence_check():
    prompt = system_prompt('review')
    for term in ('数值/时间', '最强反证', '实际字段', '已提供指标不得声称缺失'):
        assert term in prompt
    assert '不得新增事件、改写时间、设备、类别' in prompt
    assert 'contradictions=[]' in prompt


@pytest.mark.parametrize('role', ['confirmation', 'localization', 'classification', 'review'])
def test_physical_link_abstention_does_not_gate_business_confirmation(role):
    prompt = system_prompt(role)
    assert '若要判物理链路类别但只有业务症状，则对该类别 deferred' in prompt
    assert '业务故障确认不要求先确定物理机制' in prompt
    assert '只有业务症状而缺少机制证据时 deferred' not in prompt

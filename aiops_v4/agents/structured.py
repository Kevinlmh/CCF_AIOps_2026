"""Schema constrained role results; the same evidence contracts still apply."""
import re
from aiops_challenge_2026.schema import VALID_MAJOR_SUB_PAIRS, VALID_NETWORK_ELEMENTS
from .contracts import (validate_confirmation, validate_localization, validate_classification,
                        validate_review, fields, text, missing, citations)

FINAL_TOOL = 'submit_role_result'
STRUCTURED_INSTRUCTION = ('本会话使用函数参数提交结果。需要更多证据时调用只读查询工具；'
    '证据核验后调用 submit_role_result，不要用普通文本输出结论。'
    '函数参数严格遵守当前角色格式。'
    '事件确认的函数参数使用 assignments 和 assessments：assignments 必须为每个 w 编号指定 a 组编号，'
    'assessments 给出对应 group_id 的判断和证据，不再填写 window_ids。'
    '例如三个窗口同属一组：assignments={"w0":"a0","w1":"a0","w2":"a0"}，assessments=[{"group_id":"a0",...}]。'
    '候选窗口别名在 candidate_window_aliases 中；可以拆成多组，不得遗漏任何窗口。'
    'candidate_observations 是实际候选窗口的紧凑指标序列。metric_semantics 给出单位；'
    'candidate_reference_by_group 给出最初观测（未保证健康），请先看整个变化过程与该参考，'
    '再比较 local_comparison_observations 中同组前后未触发观测（不保证健康）。'
    '不要把接口零流量当作节点资源故障的主要证据。'
    '若查节点资源，query_states 指定 source="node"；其他机制指定 manifest 中对应的 source。'
    '补充查询最多两轮；不要逐分钟重复查询。预算内仍缺证据时如实 deferred。')


def compact_context(context):
    result=dict(context or {})
    knowledge=result.get('knowledge')
    if isinstance(knowledge,dict):
        result['knowledge']={k:knowledge[k] for k in ('version','source','limits') if k in knowledge}
        result['knowledge']['cards']=[{k:c[k] for k in ('major_category','sub_category','mechanism')}
                                     for c in knowledge.get('cards',[])]
    return result


def role_tools(role,payload,seen,query_turns=0):
    from .tools import tool_definitions
    return (tool_definitions(compact=True) if query_turns<2 else [])+[result_tool(role,payload,seen)]


def obj(**properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def array(items, maximum=5, minimum=0):
    return dict(type='array', items=items, minItems=minimum, maxItems=maximum)


def enum(values):
    return dict(type='string', enum=list(values))


def result_tool(role, payload, seen):
    explanation = dict(type='string', minLength=1, maxLength=240)
    score = dict(type='number', minimum=0, maximum=1)
    absent = array(explanation)
    variants = []
    for kind in ('state', 'raw', 'event', 'relation'):
        ids = sorted(i for k, i in seen if k == kind)
        if ids:
            variants.append(obj(kind=enum([kind]), id=enum(ids),
                                stance=enum(['support', 'counter', 'context']), claim=explanation))
    refs = array({'anyOf': variants} if variants else obj(), maximum=3)
    if not variants: refs['maxItems'] = 0
    windows = [w['vector_id'] for w in payload.get('manifest', [])]
    # Repeating N group enums in every window property makes the grammar O(N²).
    # Larger manifests use a linear-size syntax; exact range/ownership remains
    # validated by canonical_result. Small-case wire format remains unchanged.
    groups=['a'+str(i) for i in range(max(1,len(windows)))]
    group_type=enum(groups) if len(windows)<=32 else dict(type='string',pattern='^a(0|[1-9][0-9]*)$',
                                                        maxLength=1+len(str(len(windows)-1)))
    category = {'anyOf': [obj(major_category=enum([a]), sub_category=enum([b]))
                           for a, b in sorted(VALID_MAJOR_SUB_PAIRS)]}
    candidate=obj(network_element_id=enum(sorted(VALID_NETWORK_ELEMENTS)), confidence=score,reason=explanation,citations=refs)
    localization = {'anyOf': [
        obj(decision=enum(['resolved']),candidates=array(candidate,minimum=1),reason=explanation,missing_evidence=absent),
        obj(decision=enum(['deferred']),candidates=array(candidate,maximum=0),reason=explanation,missing_evidence=array(explanation,minimum=1))]}
    classification = {'anyOf': [
        obj(decision=enum(['resolved']),category=category,confidence=score,reason=explanation,citations=refs,missing_evidence=absent),
        obj(decision=enum(['deferred']),category={'type':'null'},confidence=score,reason=explanation,citations=refs,
            missing_evidence=array(explanation,minimum=1))]}
    schemas = dict(
        confirmation=obj(assignments=obj(**{'w'+str(i):group_type for i in range(len(windows))}),
            assessments=array(obj(group_id=group_type,decision=enum(['confirmed', 'rejected', 'deferred']),
            confidence=score, reason=explanation, citations=refs, missing_evidence=absent),
            maximum=max(1, len(windows)), minimum=1)),
        localization=localization, classification=classification,
        review=obj(decision=enum(['accept', 'reject', 'defer']), reason=explanation, citations=refs,
                   missing_evidence=absent, counterevidence_assessment=explanation, contradictions=absent),
        state_explanation=obj(summary=explanation, citations=refs, limits=absent),
        joint_diagnosis=obj(localization=localization, classification=classification))
    return dict(type='function', function=dict(name=FINAL_TOOL, strict=True,
        description='Submit the final '+role+' judgment; no side effects. Evidence and coverage are validated.',
        parameters=schemas[role]))


def canonical_result(role, arguments, payload):
    """Expand only the model's explicit complete alias assignments to real IDs."""
    if role!='confirmation':return arguments
    fields(arguments,('assignments','assessments'))
    aliases={'w'+str(i):w['vector_id'] for i,w in enumerate(payload['manifest'])}
    assignments=arguments['assignments']
    if not isinstance(assignments,dict) or set(assignments)!=set(aliases):
        raise ValueError('all candidate windows must be assigned exactly once')
    if any(not isinstance(group,str) or re.fullmatch(r'a(0|[1-9][0-9]*)',group) is None
           or int(group[1:])>=len(aliases) for group in assignments.values()):
        raise ValueError('invalid assessment group range')
    if not isinstance(arguments['assessments'],list):raise ValueError('assessments must be list')
    groups=set();result=[]
    for item in arguments['assessments']:
        fields(item,('group_id','decision','confidence','reason','citations','missing_evidence'))
        group=item['group_id']
        if not isinstance(group,str) or group in groups:raise ValueError('duplicate or invalid assessment group')
        groups.add(group)
        ids=[identifier for alias,identifier in aliases.items() if assignments[alias]==group]
        if not ids:raise ValueError('empty assessment group')
        result.append(dict({k:v for k,v in item.items() if k!='group_id'},window_ids=ids))
    if any(group not in groups for group in assignments.values()):raise ValueError('unknown assessment group')
    return dict(assessments=result)


def validate_result(role, output, payload, seen):
    if role == 'confirmation': return validate_confirmation(output, payload['manifest'], seen)
    if role == 'localization': return validate_localization(output, seen)
    if role == 'classification': return validate_classification(output, payload['event'], seen)
    if role == 'review': return validate_review(output, payload['event'], seen)
    if role == 'joint_diagnosis':
        fields(output, ('localization', 'classification'))
        validate_localization(output['localization'], seen)
        return validate_classification(output['classification'], payload['event'], seen)
    if role == 'state_explanation':
        fields(output, ('summary', 'citations', 'limits')); text(output['summary']); missing(output['limits'], True)
        if not any(c['kind'] in ('state', 'raw') for c, _ in citations(output['citations'], seen)):
            raise ValueError('observation required')


def contract_feedback(error):
    return dict(accepted=False, error=str(error), instruction=(
        '修正这个结构或证据约束错误，再调用 submit_role_result。不得补造证据或改成确认以通过校验。'
        '确认角色需在 assignments 中完整分配全部 w 窗口编号，并为使用的 a 组填写 assessments；'
        '引用只用实际见到的证据，更多证据可通过只读工具查询。'))

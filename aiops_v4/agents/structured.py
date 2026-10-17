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



def simple_protocol(payload):
    return (payload.get('bundle') or {}).get('a1_role_contract') in {'simple-v1','simple-v2'}

def device_map_protocol(payload):
    return (payload.get('bundle') or {}).get('a1_role_contract')=='simple-v2'

def role_system_prompt(role,payload,structured):
    from .prompts import system_prompt
    base=system_prompt(role)
    if role=='review' and (payload.get('bundle') or {}).get('root_ranking_policy') in {'ranked5-v3','ranked5-v4'}:
        from .prompts import COMPONENT_REVIEW_INSTRUCTION
        base+='\n'+COMPONENT_REVIEW_INSTRUCTION
    if role=='confirmation' and (payload.get('bundle') or {}).get('root_ranking_policy')=='ranked5-v4':
        base='\n'.join(line for line in base.splitlines() if not any(t in line for t in ('assignments','window_ids','assessments','分配到一个 assessment')))
    if not structured:return base
    if not device_map_protocol(payload):return base+'\n'+STRUCTURED_INSTRUCTION+contract_instruction(role,payload)
    # Present one format, without legacy JSON examples followed by overrides.
    lines=[line for line in base.splitlines() if not line.startswith('格式：')
           and not line.startswith('如果本 bundle 全部候选窗口')]
    base='\n'.join(lines).replace('最终仅输出一个 JSON 对象，不要 Markdown。字段严格遵守当前角色约定。',
        '最终调用submit_role_result提交判断，不要普通文本或Markdown；参数遵循当前工具schema。')
    instruction=('需要补证时使用只读查询工具，最多两轮。候选窗口、指标单位及前后对照均须核验；'
        '无当前触发不等于健康，缺测不等于正常。查询按实际entity_id、source及事件时段收窄。')
    if role in ('confirmation','classification','review'):
        instruction+=('本角色用support数组填写本事件窗口的正向支持证据，每条只含kind/id/claim；mechanism_support可补充实际见到的服务端资源等机制证据，counter_citations单独填写反证或上下文。'
            'confirmed、resolved或accept必须至少一条来自本事件已见窗口的state/raw支持；无此支持可rejected/deferred或defer，不要硬确认。'
            '不能将同一支持理由原样列为反证；引用的数值和观测所属设备必须与原文相符。')
    result=base+'\n'+instruction+contract_instruction(role,payload)
    if (payload.get('bundle') or {}).get('root_ranking_policy')=='ranked5-v1' and role=='localization':
        result=result.replace(('resolved必须恰好5个不同设备且priority各为1至5；不能定位首选则deferred且candidates_by_device={}。' if (payload.get('bundle') or {}).get('root_ranking_policy')=='ranked5-v1' else '不必凑满5台；证据不足时decision=deferred、candidates_by_device={}并说明missing_evidence。'),
            '本次resolved必须恰好5台不同设备，priority分别为1、2、3、4、5。root_candidate_context是同时间的真实设备观测，只供候选对比，不自动证明故障。首选必须有正向因果依据；备选是低概率假设，明确其支持、反证和未知，不声称备选已经故障。support引用其本机观测，claim须如实写观测如何影响候选排序。首选仍不能定位时deferred，不捏造设备或数据。')
    return result

EVIDENCE_FIRST_KEYS={'window_ids':'a_windows','support':'b_support','mechanism_support':'c_mechanism_support',
    'counter_citations':'d_counter_citations','reason':'e_reason','decision':'z_decision',
    'network_element_id':'a_device','preferred_device':'z_preferred_device','category':'zz_category','missing_evidence':'zz_missing_evidence','id':'a_id','kind':'b_kind','claim':'c_claim'}

def evidence_first_schema(schema):
    if isinstance(schema,list):return [evidence_first_schema(v) for v in schema]
    if not isinstance(schema,dict):return schema
    result={}
    for key,value in schema.items():
        if key=='properties':result[key]={EVIDENCE_FIRST_KEYS.get(k,k):evidence_first_schema(v) for k,v in value.items()}
        elif key=='required':result[key]=[EVIDENCE_FIRST_KEYS.get(k,k) for k in value]
        else:result[key]=evidence_first_schema(value)
    return result

def evidence_first_arguments(value):
    if isinstance(value,list):return [evidence_first_arguments(v) for v in value]
    if not isinstance(value,dict):return value
    reverse={v:k for k,v in EVIDENCE_FIRST_KEYS.items()};result={}
    for key,item in value.items():
        target=reverse.get(key,key)
        if target in result:raise ValueError('duplicate canonical role field')
        result[target]=evidence_first_arguments(item)
    return result

def evidence_first(payload):
    return (payload.get('bundle') or {}).get('root_ranking_policy') in {'ranked5-v2','ranked5-v3','ranked5-v4'}

def contract_instruction(role,payload):
    if not simple_protocol(payload):return ''
    if evidence_first(payload):
        common='\n本次工具按证据在前、判断在后的字段组织：b_support=正证，c_mechanism_support=机制补证，d_counter_citations=反证/上下文，e_reason=解释，z_decision=最后决定；zz_category和zz_missing_evidence在决定后填写；引用内a_id为实际ID、b_kind为来源类型、c_claim为真实观测。只使用当前schema字段。'
        if role=='confirmation' and (payload.get('bundle') or {}).get('root_ranking_policy')=='ranked5-v4':return common+'component_assessments对candidate_components中的每个c编号恰好填写一项真假故障判断。窗口按观测时间连通性预归组且完整保留，不输出窗口清单或assignments，不得重复分组。若同一连续片段内疑似多个不同故障而无法统一，应deferred说明需要细分，不编造单一原因。'
        if role=='confirmation':return common+'assessments每项自带a_windows：同一个连续事件只写一项，a_windows=["*"]覆盖全部；需要拆组则每项填写其实际w别名列表。全部窗口必须恰好覆盖一次，不输出assignments或组编号。不可按指标重复分组；先确定窗口和正反证，再写e_reason，最后z_decision，正常波动应rejected。'
        if role=='localization':return common+'本次candidates_by_device对工具列出的每台真实候选设备分别评分，所有设备键都必须填写且每台仅一项。每项confidence是相对因果可能性，b_support只引该设备已见观测，e_reason必须区分强首选与较弱备选/反证；相同confidence按设备ID稳定排序。逐设备核验后用z_preferred_device指定最可能的首选；程序保留这个首选，其余按confidence排序取4个备选，共5台不同设备，不随机补位、不把备选说成故障。全部候选评分后仍无法定位，可如实deferred并令z_preferred_device=null；本轮保留评分表，程序不导出未定位结果。endpoint_associations是用户从实际exporter/NetFlow交叉核对的请求目标关联，不是每次故障答案；服务端CPU正常不能否定应用故障，正常且未关联的路由器不能仅因没有其他异常就排第一。'
        return common
    if role=='confirmation':
        return ('\n本次A1工具格式覆盖前述示例：assessments数组第i项就是组ai，不输出group_id。'
                '所有窗口属于一个连续观测事件时，assignments直接填字符串all，并且assessments恰好一项。'
                '确需拆为多个事件时，assignments仍逐一填写w编号到ai；每项assessment必须拥有窗口。'
                '按事件分组，不按指标分组；同一时段同一设备的CPU和load可以是一个事件的多项证据，不能仅因两个指标就重复报事件。'
                '这是分组格式简化，仍须独立决定confirmed/rejected/deferred，不能为格式通过制造故障。')
    if role=='localization' and device_map_protocol(payload):
        return ('\n根因工具格式：decision、candidates_by_device、reason、missing_evidence。'
                'candidates_by_device是按官方设备ID作键的对象；每台设备最多一个条目，最多选择5台。'
                '每条含priority(1至5，越小越靠前，并列按设备ID排序)、confidence、reason、support数组(1至3条)、counter_citations数组。'
                '同一设备的CPU、磁盘、内存是同一个候选的多项证据，不能作为多个候选。'
                'support每项为{kind,id,claim}，只能引用这个设备自己的已见state/raw；反证及上下文放counter_citations。' +
                ('resolved必须恰好5个不同设备且priority各为1至5；不能定位首选则deferred且candidates_by_device={}。' if (payload.get('bundle') or {}).get('root_ranking_policy')=='ranked5-v1' else '不必凑满5台；证据不足时decision=deferred、candidates_by_device={}并说明missing_evidence。') +
                '端点地址关联不是因果证明；不能把客户端业务症状直接当作服务端资源异常。')
    if role=='localization':
        return ('\n本次A1工具格式覆盖前述示例：每个candidate用support={kind,id,claim}选择该设备自己的实际证据，'
                'counter_citations只填反证或上下文。设备与support的对应由工具schema绑定。'
                '不能将另一台设备的业务症状当作这个设备自己的证据，端点地址关联仍不是因果证明；证据不足可deferred。')
    return ''

def role_tools(role,payload,seen,query_turns=0,*,chunks=None):
    from .tools import tool_definitions
    tools=tool_definitions(compact=True) if query_turns<2 else []
    if (payload.get('bundle') or {}).get('initial_evidence_profile')=='a1-readable-1':
        groups=sorted({row['group_id'] for (kind,_),row in seen.items() if kind=='state' and isinstance(row.get('group_id'),str)})
        for tool in tools:
            function=tool['function']
            if function['name']=='query_states':
                function['parameters']['properties']['detail']=dict(type='string',enum=['summary','full'])
                if groups:
                    function['parameters']['properties']['group_id']=dict(type='string',enum=groups)
                    function['description']+=' group_id must be a delivered state group; a bundle id is not a group.'
                if (payload.get('bundle') or {}).get('root_ranking_policy')=='ranked5-v4':
                    for key in ('start','end'):
                        function['parameters']['properties'][key]=dict(type='string',pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$')
                function['description']+=' Default detail=summary: complete concise measurements, validity and deltas, with omitted fields declared. detail=full returns complete counter details and may require read_chunk; incomplete data cannot be cited.'
        pending={key:row['next'] for key,row in (chunks or {}).items() if row.get('next',0)<row.get('total',len(row.get('text','')))}
        filtered=[]
        for tool in tools:
            f=tool['function']
            if f['name']=='query_model':
                if not groups:continue
                f['parameters']['properties']['group_id']=dict(type='string',enum=groups)
            if f['name']=='read_chunk':
                if not pending:continue
                f['parameters']['properties']['handle']=dict(type='string',enum=sorted(pending))
                f['parameters']['properties']['offset']=dict(type='integer',enum=sorted(set(pending.values())))
            filtered.append(tool)
        tools=filtered
    return tools+[result_tool(role,payload,seen)]


def obj(**properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def array(items, maximum=5, minimum=0):
    return dict(type='array', items=items, minItems=minimum, maxItems=maximum)


def enum(values):
    return dict(type='string', enum=list(values))



def available_link_subcategories(seen):
    """Expose physical labels only after corresponding measured mechanisms are delivered."""
    import math
    mechanisms=set()
    for (kind,_),row in seen.items():
        if kind!='state':continue
        for name,cell in row.get('feature_values',{}).items():
            value=cell.get('raw_value') if isinstance(cell,dict) else None
            if type(value) not in (int,float) or not math.isfinite(value) or value<=0:continue
            key=name.lower()
            if any(s in key for s in ('packet_loss','rx_drop_rate','tx_drop_rate','rx_error_rate','tx_error_rate')):mechanisms.add('loss')
            if any(s in key for s in ('rtt','path_delay','icmp_latency','ping_latency')):mechanisms.add('delay')
            if any(s in key for s in ('bandwidth_limit','rate_limit','shaper_limit')):mechanisms.add('limit')
    return {sub for major,sub in VALID_MAJOR_SUB_PAIRS if major=='link' and
            (('loss' in sub and 'loss' in mechanisms) or ('delay' in sub and 'delay' in mechanisms) or
             ('loss' not in sub and 'delay' not in sub and 'limit' in mechanisms))}


def eligible_root_devices(seen):
    from .tools import entity_of
    devices=set()
    for key,row in seen.items():
        kind,_=key
        if kind not in ('state','raw'):continue
        entity=entity_of(row)
        if entity not in VALID_NETWORK_ELEMENTS:continue
        if entity.endswith('-traffic-vm'):
            resource=kind=='state' and row.get('source')=='node' and row.get('candidate') is True
            path=kind=='state' and row.get('source')!='traffic' and bool(available_link_subcategories({key:row}))
            raw=kind=='raw' and any(s in str(row.get('source_file','')) for s in ('node_metrics','interface_metrics','frr_syslog'))
            if not (resource or path or raw):continue
        devices.add(entity)
    return sorted(devices)

def result_tool(role, payload, seen):
    explanation = dict(type='string', minLength=1, maxLength=240)
    score = dict(type='number', minimum=0, maximum=1)
    absent = array(explanation)
    variants = []
    for kind in ('state', 'raw', 'event', 'relation'):
        ids = sorted(i for k, i in seen if k == kind)
        if ids:
            scoped=(payload.get('bundle') or {}).get('initial_evidence_profile')=='a1-readable-1' and role in ('confirmation','classification','review')
            if scoped:
                selected={w['vector_id'] for w in payload.get('manifest',[])} if role=='confirmation' else set((payload.get('event') or {}).get('window_ids',[]))
                eligible=selected if kind=='state' else ({r['record_id'] for identifier in selected for r in seen.get(('state',identifier),{}).get('references',[])} if kind=='raw' else set())
                supported=[identifier for identifier in ids if identifier in eligible]
                if supported:variants.append(obj(kind=enum([kind]),id=enum(supported),stance=enum(['support']),claim=explanation))
                variants.append(obj(kind=enum([kind]),id=enum(ids),stance=enum(['counter','context']),claim=explanation))
            else:
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
    readable=(payload.get('bundle') or {}).get('initial_evidence_profile')=='a1-readable-1'
    allowed_links=available_link_subcategories(seen) if readable else None
    category = {'anyOf': [obj(major_category=enum([a]), sub_category=enum([b]))
                           for a, b in sorted(VALID_MAJOR_SUB_PAIRS) if allowed_links is None or a!='link' or b in allowed_links]}
    roots=eligible_root_devices(seen) if readable else sorted(VALID_NETWORK_ELEMENTS)
    candidate=obj(network_element_id=enum(roots or sorted(VALID_NETWORK_ELEMENTS)), confidence=score,reason=explanation,citations=refs)
    localization = {'anyOf': [
        obj(decision=enum(['resolved']),candidates=array(candidate,minimum=1),reason=explanation,missing_evidence=absent),
        obj(decision=enum(['deferred']),candidates=array(candidate,maximum=0),reason=explanation,missing_evidence=array(explanation,minimum=1))]}
    classification = {'anyOf': [
        obj(decision=enum(['resolved']),category=category,confidence=score,reason=explanation,citations=refs,missing_evidence=absent),
        obj(decision=enum(['deferred']),category={'type':'null'},confidence=score,reason=explanation,citations=refs,
            missing_evidence=array(explanation,minimum=1))]}
    if readable and not roots:localization['anyOf']=localization['anyOf'][1:]
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
    if simple_protocol(payload):
        if role=='confirmation':
            item=obj(decision=enum(['confirmed','rejected','deferred']),confidence=score,reason=explanation,citations=refs,missing_evidence=absent)
            schemas['confirmation']={'anyOf':[
                obj(assignments=enum(['all']),assessments=array(item,minimum=1,maximum=1)),
                obj(assignments=obj(**{'w'+str(i):group_type for i in range(len(windows))}),
                    assessments=array(item,minimum=1,maximum=max(1,len(windows))))]}
        if role=='localization':
            from .tools import entity_of
            counter_variants=[]
            for kind in ('state','raw','event','relation'):
                ids=sorted(identifier for k,identifier in seen if k==kind)
                if ids:counter_variants.append(obj(kind=enum([kind]),id=enum(ids),stance=enum(['counter','context']),claim=explanation))
            counter_refs=array({'anyOf':counter_variants} if counter_variants else obj(),maximum=2)
            if not counter_variants:counter_refs['maxItems']=0
            alternatives=[]
            for node in roots:
                proof=[]
                for kind in ('state','raw'):
                    ids=sorted(identifier for (k,identifier),row in seen.items() if k==kind and entity_of(row)==node)
                    if ids:proof.append(obj(kind=enum([kind]),id=enum(ids),claim=explanation))
                if proof:alternatives.append(obj(network_element_id=enum([node]),confidence=score,reason=explanation,
                    support={'anyOf':proof},counter_citations=counter_refs))
            candidate_items={'anyOf':alternatives} if alternatives else obj()
            localization_choices=[obj(decision=enum(['deferred']),candidates=array(candidate_items,maximum=0),
                reason=explanation,missing_evidence=array(explanation,minimum=1))]
            if alternatives:localization_choices.insert(0,obj(decision=enum(['resolved']),candidates=array(candidate_items,minimum=1),
                reason=explanation,missing_evidence=absent))
            schemas['localization']={'anyOf':localization_choices}
            if device_map_protocol(payload):
                by_device={}
                for alternative in alternatives:
                    props=alternative['properties'];node=props['network_element_id']['enum'][0]
                    by_device[node]=obj(priority=dict(type='integer',minimum=1,maximum=5),
                        confidence=score,reason=explanation,support=array(props['support'],minimum=1,maximum=3),
                        counter_citations=counter_refs)
                # Distinct object keys, rather than array entries, encode device uniqueness.
                device_schema=dict(type='object',properties=by_device,required=[],additionalProperties=False)
                choices=[obj(decision=enum(['deferred']),candidates_by_device=obj(),reason=explanation,
                             missing_evidence=array(explanation,minimum=1))]
                if by_device:choices.insert(0,obj(decision=enum(['resolved']),candidates_by_device=device_schema,
                             reason=explanation,missing_evidence=absent))
                schemas['localization']={'anyOf':choices}

    if readable and role=='review':
        schemas['review']={'anyOf':[
            obj(decision=enum(['accept']),reason=explanation,citations=refs,missing_evidence=absent,
                counterevidence_assessment=explanation,contradictions=array(explanation,maximum=0)),
            obj(decision=enum(['reject','defer']),reason=explanation,citations=refs,missing_evidence=absent,
                counterevidence_assessment=explanation,contradictions=absent)]}
    if device_map_protocol(payload) and role in ('confirmation','classification','review'):
        selected={w['vector_id'] for w in payload.get('manifest',[])} if role=='confirmation' else set((payload.get('event') or {}).get('window_ids',[]))
        event_raw={ref['record_id'] for identifier in selected for ref in seen.get(('state',identifier),{}).get('references',[])}
        positive_variants=[];negative_variants=[];mechanism_variants=[]
        for kind in ('state','raw','event','relation'):
            ids=sorted(identifier for k,identifier in seen if k==kind)
            eligible=selected if kind=='state' else event_raw if kind=='raw' else set()
            positive=[identifier for identifier in ids if identifier in eligible]
            if positive:positive_variants.append(obj(kind=enum([kind]),id=enum(positive),claim=explanation))
            if ids and kind in ('state','raw'):mechanism_variants.append(obj(kind=enum([kind]),id=enum(ids),claim=explanation))
            if ids:negative_variants.append(obj(kind=enum([kind]),id=enum(ids),stance=enum(['counter','context']),claim=explanation))
        def supports(required=False):
            return array({'anyOf':positive_variants} if positive_variants else obj(),
                         minimum=1 if required else 0,maximum=3 if positive_variants else 0)
        mechanism=array({'anyOf':mechanism_variants} if mechanism_variants else obj(),maximum=2 if mechanism_variants else 0)
        counters=array({'anyOf':negative_variants} if negative_variants else obj(),maximum=3 if negative_variants else 0)
        if role=='confirmation':
            def assessment(decision):
                return obj(decision=enum([decision]),confidence=score,reason=explanation,
                    support=supports(decision=='confirmed'),mechanism_support=mechanism,counter_citations=counters,
                    missing_evidence=array(explanation,minimum=1) if decision=='deferred' else absent)
            options=[assessment(d) for d in ('confirmed','rejected','deferred') if d!='confirmed' or positive_variants]
            item={'anyOf':options}
            schemas[role]={'anyOf':[
                obj(assignments=enum(['all']),assessments=array(item,minimum=1,maximum=1)),
                obj(assignments=obj(**{'w'+str(i):group_type for i in range(len(windows))}),
                    assessments=array(item,minimum=1,maximum=max(1,len(windows))))]}
        elif role=='classification':
            options=[obj(decision=enum(['deferred']),category={'type':'null'},confidence=score,
                         reason=explanation,support=supports(),mechanism_support=mechanism,counter_citations=counters,missing_evidence=array(explanation,minimum=1))]
            if positive_variants:options.insert(0,obj(decision=enum(['resolved']),category=category,confidence=score,
                         reason=explanation,support=supports(True),mechanism_support=mechanism,counter_citations=counters,missing_evidence=absent))
            schemas[role]={'anyOf':options}
        else:
            options=[obj(decision=enum([decision]),reason=explanation,support=supports(),
                mechanism_support=mechanism,counter_citations=counters,missing_evidence=array(explanation,minimum=1) if decision=='defer' else absent,
                counterevidence_assessment=explanation,contradictions=absent) for decision in ('reject','defer')]
            if positive_variants:options.insert(0,obj(decision=enum(['accept']),reason=explanation,support=supports(True),
                mechanism_support=mechanism,counter_citations=counters,missing_evidence=absent,counterevidence_assessment=explanation,
                contradictions=array(explanation,maximum=0)))
            schemas[role]={'anyOf':options}
    if evidence_first(payload):
        if role=='localization':
            from .tools import entity_of
            choices=[]
            for kind in ('state','raw'):
                ids=sorted(identifier for (k,identifier),row in seen.items() if k==kind and entity_of(row) in roots)
                if ids:choices.append(obj(kind=enum([kind]),id=enum(ids),claim=explanation))
            scored={}
            for node,definition in by_device.items():
                props=definition['properties']
                scored[node]=obj(confidence=score,reason=explanation,
                    support=array(props['support']['items'],minimum=1,maximum=2),counter_citations=counter_refs)
            options=[obj(decision=enum(['deferred']),candidates_by_device=obj(),preferred_device={'type':'null'},reason=explanation,missing_evidence=array(explanation,minimum=1))]
            if len(scored)>=5:options.insert(0,obj(decision=enum(['resolved']),candidates_by_device=obj(**scored),preferred_device=enum(scored),reason=explanation,missing_evidence=absent))
            schemas[role]={'anyOf':options}
            if (payload.get('bundle') or {}).get('root_ranking_policy') in {'ranked5-v3','ranked5-v4'}:
                options=[obj(decision=enum(['deferred']),candidates_by_device=obj(**scored),preferred_device={'type':'null'},reason=explanation,missing_evidence=array(explanation,minimum=1))]
                if len(scored)>=5:options.insert(0,obj(decision=enum(['resolved']),candidates_by_device=obj(**scored),preferred_device=enum(scored),reason=explanation,missing_evidence=absent))
                schemas[role]={'anyOf':options}
        if role=='confirmation':
            def grouped_assessment(decision):
                return obj(window_ids=array(enum(['*']+['w'+str(i) for i in range(len(windows))]),minimum=1,maximum=max(1,len(windows))),
                    decision=enum([decision]),confidence=score,reason=explanation,
                    support=supports(decision=='confirmed'),mechanism_support=mechanism,counter_citations=counters,
                    missing_evidence=array(explanation,minimum=1) if decision=='deferred' else absent)
            options=[grouped_assessment(d) for d in ('confirmed','rejected','deferred') if d!='confirmed' or positive_variants]
            schemas[role]=obj(assessments=array({'anyOf':options},minimum=1,maximum=max(1,len(windows))))
        if role=='review' and (payload.get('bundle') or {}).get('root_ranking_policy') in {'ranked5-v3','ranked5-v4'}:
            from .review_basis import first_root,causal_bases
            from .tools import entity_of
            root=first_root(payload);bases=sorted(causal_bases(payload,seen));proofs=[]
            for kind in ('state','raw'):
                ids=sorted(i for (k,i),row in seen.items() if k==kind and entity_of(row)==root)
                if ids:proofs.append(obj(kind=enum([kind]),id=enum(ids),claim=explanation))
            own=array({'anyOf':proofs} if proofs else obj(),minimum=1 if proofs else 0,maximum=2 if proofs else 0)
            root_options=[obj(verdict=enum(['insufficient','contradicted']),basis=enum(bases+['unestablished']),reason=explanation,proofs=own)]
            from .review_basis import basis_witnesses
            witnesses=basis_witnesses(payload,seen)
            for basis,keys in sorted(witnesses.items()):
                parts=[]
                for kind in ('state','raw'):
                    ids=sorted(i for k,i in keys if k==kind)
                    if ids:parts.append(obj(kind=enum([kind]),id=enum(ids),claim=explanation))
                if parts:root_options.insert(0,obj(verdict=enum(['supported']),basis=enum([basis]),reason=explanation,
                    proofs=array({'anyOf':parts},minimum=1,maximum=2)))
            def reviewed(decision):
                return obj(decision=enum([decision]),reason=explanation,support=supports(decision=='accept'),
                    mechanism_support=mechanism,counter_citations=counters,
                    missing_evidence=array(explanation,minimum=1) if decision=='defer' else absent,
                    counterevidence_assessment=explanation,contradictions=array(explanation,maximum=0) if decision=='accept' else absent,
                    root_check={'anyOf':[v for v in root_options if v['properties']['verdict']['enum']==['supported']]} if decision=='accept' else {'anyOf':root_options},
                    category_check=obj(verdict=enum(['supported']) if decision=='accept' else enum(['supported','insufficient','contradicted']),reason=explanation,proofs=supports(True)))
            schemas[role]={'anyOf':[reviewed(d) for d in ('accept','reject','defer') if d!='accept' or witnesses]}
        if role=='confirmation' and (payload.get('bundle') or {}).get('root_ranking_policy')=='ranked5-v4':
            from .partition import observed_components
            component_schemas={}
            for component in observed_components(payload['manifest']):
                selected=set(component['window_ids']);raw_ids={r['record_id'] for i in selected for r in seen.get(('state',i),{}).get('references',[])}
                parts=[]
                for kind,eligible in (('state',selected),('raw',raw_ids)):
                    ids=sorted(i for k,i in seen if k==kind and i in eligible)
                    if ids:parts.append(obj(kind=enum([kind]),id=enum(ids),claim=explanation))
                def component_item(decision):
                    return obj(decision=enum([decision]),confidence=score,reason=explanation,
                        support=array({'anyOf':parts} if parts else obj(),minimum=1 if decision=='confirmed' else 0,maximum=3 if parts else 0),
                        mechanism_support=mechanism,counter_citations=counters,
                        missing_evidence=array(explanation,minimum=1) if decision=='deferred' else absent)
                component_schemas[component['key']]={'anyOf':[component_item(d) for d in ('confirmed','rejected','deferred') if d!='confirmed' or parts]}
            schemas[role]=obj(component_assessments=obj(**component_schemas))
        schemas[role]=evidence_first_schema(schemas[role])
    return dict(type='function', function=dict(name=FINAL_TOOL, strict=True,
        description='Submit the final '+role+' judgment; no side effects. Evidence and coverage are validated.',
        parameters=schemas[role]))


def explicit_citations(support,counter_citations):
    if not isinstance(support,list) or len(support)>5:raise ValueError('support must contain at most five observations')
    if not isinstance(counter_citations,list) or len(counter_citations)>3:raise ValueError('counter citations budget')
    for proof in support:
        fields(proof,('kind','id','claim'))
        if proof['kind'] not in ('state','raw'):raise ValueError('support needs actual state/raw')
    for proof in counter_citations:
        fields(proof,('kind','id','stance','claim'))
        if proof['stance'] not in ('counter','context'):raise ValueError('counter citations cannot add support')
    return [dict(proof,stance='support') for proof in support]+counter_citations

def canonical_result(role, arguments, payload, seen=None):
    """Expand only the model's explicit complete alias assignments to real IDs."""
    if evidence_first(payload):
        arguments=evidence_first_arguments(arguments)
        if role=='confirmation' and (payload.get('bundle') or {}).get('root_ranking_policy')=='ranked5-v4':
            from .partition import observed_components
            fields(arguments,('component_assessments',));groups=observed_components(payload['manifest']);items=arguments['component_assessments']
            if not isinstance(items,dict) or set(items)!={g['key'] for g in groups}:raise ValueError('every observed component requires exactly one judgment')
            result=[]
            for group in groups:
                row=items[group['key']];fields(row,('decision','confidence','reason','support','mechanism_support','counter_citations','missing_evidence'))
                result.append(dict(window_ids=group['window_ids'],decision=row['decision'],confidence=row['confidence'],reason=row['reason'],
                    citations=explicit_citations(row['support']+row['mechanism_support'],row['counter_citations']),missing_evidence=row['missing_evidence']))
            return dict(assessments=result)
        if role=='confirmation':
            fields(arguments,('assessments',))
            aliases={'w'+str(i):w['vector_id'] for i,w in enumerate(payload['manifest'])}
            result=[]
            for item in arguments['assessments']:
                fields(item,('window_ids','decision','confidence','reason','support','mechanism_support','counter_citations','missing_evidence'))
                ids=item['window_ids']
                if ids==['*']:selected=ids
                else:
                    if not isinstance(ids,list) or not ids or any(w not in aliases for w in ids):raise ValueError('each assessment needs actual w aliases, or one ["*"] group')
                    selected=[aliases[w] for w in ids]
                result.append(dict(window_ids=selected,decision=item['decision'],confidence=item['confidence'],reason=item['reason'],
                    citations=explicit_citations(item['support']+item['mechanism_support'],item['counter_citations']),missing_evidence=item['missing_evidence']))
            return dict(assessments=result)
        if role=='localization':
            fields(arguments,('decision','candidates_by_device','preferred_device','reason','missing_evidence'))
            candidates=arguments['candidates_by_device']
            if not isinstance(candidates,dict):raise ValueError('ranked candidate map required')
            if arguments['decision']=='resolved' and len(candidates)<5:raise ValueError('at least five distinct observed candidates required')
            if arguments['decision']=='deferred' and candidates and (payload.get('bundle') or {}).get('root_ranking_policy') not in {'ranked5-v3','ranked5-v4'}:raise ValueError('deferred must abstain')
            from .contracts import confidence
            result=[]
            for node,row in candidates.items():
                if node not in VALID_NETWORK_ELEMENTS:raise ValueError('unknown root device')
                fields(row,('confidence','reason','support','counter_citations'));confidence(row['confidence'])
                result.append(dict(network_element_id=node,confidence=row['confidence'],reason=row['reason'],
                    citations=explicit_citations(row['support'],row['counter_citations'])))
            preferred=arguments['preferred_device']
            if arguments['decision']=='resolved' and preferred not in candidates:raise ValueError('preferred device must be one of the actually scored devices')
            if arguments['decision']=='deferred' and preferred is not None:raise ValueError('deferred requires null preferred device')
            result.sort(key=lambda r:(r['network_element_id']!=preferred,-r['confidence'],r['network_element_id']))
            return dict(decision=arguments['decision'],candidates=result[:5] if arguments['decision']=='resolved' else [],reason=arguments['reason'],missing_evidence=arguments['missing_evidence'])
    if device_map_protocol(payload) and role=='localization':
        fields(arguments,('decision','candidates_by_device','reason','missing_evidence'))
        roots=arguments['candidates_by_device']
        if not isinstance(roots,dict) or len(roots)>5:raise ValueError('at most five distinct root devices required')
        ranked5=(payload.get('bundle') or {}).get('root_ranking_policy')=='ranked5-v1'
        if ranked5 and arguments['decision']=='resolved':
            if len(roots)!=5:raise ValueError('ranked5 resolved result requires exactly five distinct observed root devices')
            if {item.get('priority') for item in roots.values()}!={1,2,3,4,5}:
                raise ValueError('five distinct priorities 1..5 required')
        candidates=[]
        for node,item in roots.items():
            if node not in VALID_NETWORK_ELEMENTS:raise ValueError('unknown root device: '+str(node))
            fields(item,('priority','confidence','reason','support','counter_citations'))
            priority=item['priority']
            if type(priority) is not int or not 1<=priority<=5:raise ValueError('root priority must be integer 1..5')
            if not isinstance(item['support'],list) or not 1<=len(item['support'])<=3:
                raise ValueError('root support requires 1..3 actual observations')
            for proof in item['support']:fields(proof,('kind','id','claim'))
            if not isinstance(item['counter_citations'],list):raise ValueError('counter citations must be list')
            if any(c.get('stance') not in ('counter','context') for c in item['counter_citations']):
                raise ValueError('counter citations cannot add support')
            citations=[dict(proof,stance='support') for proof in item['support']]+item['counter_citations']
            candidates.append((priority,node,dict(network_element_id=node,confidence=item['confidence'],
                               reason=item['reason'],citations=citations)))
        return dict(decision=arguments['decision'],candidates=[row[2] for row in sorted(candidates,key=lambda r:r[:2])],
                    reason=arguments['reason'],missing_evidence=arguments['missing_evidence'])
    if simple_protocol(payload) and role=='localization':
        fields(arguments,('decision','candidates','reason','missing_evidence'))
        candidates=[]
        if not isinstance(arguments['candidates'],list):raise ValueError('candidates must be list')
        for item in arguments['candidates']:
            fields(item,('network_element_id','confidence','reason','support','counter_citations'))
            fields(item['support'],('kind','id','claim'))
            if not isinstance(item['counter_citations'],list):raise ValueError('counter citations must be list')
            citations=[dict(item['support'],stance='support')]+item['counter_citations']
            candidates.append(dict(network_element_id=item['network_element_id'],confidence=item['confidence'],reason=item['reason'],citations=citations))
        return dict(arguments,candidates=candidates)
    if role=='review' and (payload.get('bundle') or {}).get('root_ranking_policy') in {'ranked5-v3','ranked5-v4'}:
        from .review_basis import first_root,causal_bases
        from .tools import entity_of
        arguments=dict(arguments);checks={k:arguments.pop(k) for k in ('root_check','category_check')}
        for key,check in checks.items():
            fields(check,('verdict','reason','proofs','basis') if key=='root_check' else ('verdict','reason','proofs'))
            if check['verdict'] not in ('supported','insufficient','contradicted'):raise ValueError('invalid component verdict')
            text(check['reason'])
            if not isinstance(check['proofs'],list) or not check['proofs']:raise ValueError('component review requires actual proof')
            for proof in check['proofs']:
                fields(proof,('kind','id','claim'));row=(seen or {}).get((proof['kind'],proof['id']))
                if row is None:raise ValueError('component proof not delivered')
                if key=='root_check' and entity_of(row)!=first_root(payload):raise ValueError('root review proof must belong to Top1 device')
        root=checks['root_check'];bases=causal_bases(payload,seen or {})
        if root['basis']!='unestablished' and root['basis'] not in bases:raise ValueError('root basis not present in delivered facts')
        if root['verdict']=='supported':
            from .review_basis import basis_witnesses
            allowed=basis_witnesses(payload,seen or {}).get(root['basis'],set())
            if not allowed or any((p['kind'],p['id']) not in allowed for p in root['proofs']):
                raise ValueError('normal or unrelated node has no cited causal basis; root check must remain insufficient')
        if arguments['decision']=='accept' and any(c['verdict']!='supported' for c in checks.values()):raise ValueError('accept requires independently supported root AND category checks; otherwise defer or reject honestly')
    if device_map_protocol(payload) and role in ('classification','review'):
        expected=('decision','category','confidence','reason','support','mechanism_support','counter_citations','missing_evidence') if role=='classification' else ('decision','reason','support','mechanism_support','counter_citations','missing_evidence','counterevidence_assessment','contradictions')
        fields(arguments,expected)
        return dict({k:v for k,v in arguments.items() if k not in ('support','mechanism_support','counter_citations')},
                    citations=explicit_citations(arguments['support']+arguments['mechanism_support'],arguments['counter_citations']))
    if role!='confirmation':return arguments
    fields(arguments,('assignments','assessments'))
    aliases={'w'+str(i):w['vector_id'] for i,w in enumerate(payload['manifest'])}
    assignments=arguments['assignments']
    if simple_protocol(payload) and assignments=='all':
        if not isinstance(arguments['assessments'],list) or len(arguments['assessments'])!=1:raise ValueError('all marker requires exactly one assessment')
        assignments={alias:'a0' for alias in aliases}
    if not isinstance(assignments,dict) or set(assignments)!=set(aliases):
        raise ValueError('all candidate windows must be assigned exactly once')
    if any(not isinstance(group,str) or re.fullmatch(r'a(0|[1-9][0-9]*)',group) is None
           or int(group[1:])>=len(aliases) for group in assignments.values()):
        raise ValueError('invalid assessment group range')
    if not isinstance(arguments['assessments'],list):raise ValueError('assessments must be list')
    groups=set();result=[]
    for position,item in enumerate(arguments['assessments']):
        if device_map_protocol(payload):
            fields(item,('decision','confidence','reason','support','mechanism_support','counter_citations','missing_evidence'))
            item=dict({k:v for k,v in item.items() if k not in ('support','mechanism_support','counter_citations')},
                      citations=explicit_citations(item['support']+item['mechanism_support'],item['counter_citations']))

        if simple_protocol(payload):
            fields(item,('decision','confidence','reason','citations','missing_evidence'));group='a'+str(position)
        else:
            fields(item,('group_id','decision','confidence','reason','citations','missing_evidence'));group=item['group_id']
        if not isinstance(group,str) or group in groups:raise ValueError('duplicate or invalid assessment group')
        groups.add(group)
        ids=[identifier for alias,identifier in aliases.items() if assignments[alias]==group]
        if not ids:raise ValueError('empty assessment group')
        result.append(dict({k:v for k,v in item.items() if k!='group_id'},window_ids=ids))
    if any(group not in groups for group in assignments.values()):raise ValueError('unknown assessment group')
    return dict(assessments=result)


def _readable_result_guards(role,output,payload,seen):
    if (payload.get('bundle') or {}).get('initial_evidence_profile')=='a1-readable-1':
        if role=='localization' and output.get('decision')=='resolved':
            if device_map_protocol(payload):
                from .tools import entity_of
                for candidate in output['candidates']:
                    for proof in candidate['citations']:
                        if proof['stance']=='support' and (proof['kind'] not in ('state','raw') or
                                entity_of(seen.get((proof['kind'],proof['id']),{}))!=candidate['network_element_id']):
                            raise ValueError('every root support must belong to its selected device')

            eligible=set(eligible_root_devices(seen))
            if any(row.get('network_element_id') not in eligible for row in output.get('candidates',[])):
                raise ValueError('client root needs its own observed non-probe mechanism evidence')
        if role=='classification' and output.get('decision')=='resolved' and (output.get('category') or {}).get('major_category')=='link':
            cited={(item.get('kind'),item.get('id')):seen[(item.get('kind'),item.get('id'))] for item in output.get('citations',[]) if (item.get('kind'),item.get('id')) in seen}
            if (output.get('category') or {}).get('sub_category') not in available_link_subcategories(cited):
                raise ValueError('physical link category requires cited measured mechanism; business counter deficits alone are insufficient')

def validate_result(role, output, payload, seen):
    if role == 'confirmation': return validate_confirmation(output, payload['manifest'], seen)
    if role == 'localization':
        result=validate_localization(output,seen);_readable_result_guards(role,output,payload,seen);return result
    if role == 'classification':
        result=validate_classification(output,payload['event'],seen);_readable_result_guards(role,output,payload,seen);return result
    if role == 'review': return validate_review(output, payload['event'], seen)
    if role == 'joint_diagnosis':
        fields(output, ('localization', 'classification'))
        validate_localization(output['localization'], seen)
        return validate_classification(output['classification'], payload['event'], seen)
    if role == 'state_explanation':
        fields(output, ('summary', 'citations', 'limits')); text(output['summary']); missing(output['limits'], True)
        if not any(c['kind'] in ('state', 'raw') for c, _ in citations(output['citations'], seen)):
            raise ValueError('observation required')


def contract_feedback(error,role=None,payload=None):
    if payload is not None and simple_protocol(payload):
        return dict(accepted=False,error=str(error),instruction='修正当前角色的结构或证据错误后重试；不能捏造证据、删去真实矛盾或强行确认以通过校验。'+contract_instruction(role,payload))
    return dict(accepted=False, error=str(error), instruction=(
        '修正这个结构或证据约束错误，再调用 submit_role_result。不得补造证据或改成确认以通过校验。'
        '确认角色需在 assignments 中完整分配全部 w 窗口编号，并为使用的 a 组填写 assessments；'
        '引用只用实际见到的证据，更多证据可通过只读工具查询。'))

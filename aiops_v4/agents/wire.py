"""Lossless evidence tables and per-request reference aliases at the HTTP boundary."""
import hashlib
import re
from .jsonio import dumps,loads

VERSION='v4-evidence-wire-1'
REFERENCE_VERSION='v4-evidence-wire-2'
SUPPORTED_VERSIONS=(VERSION,REFERENCE_VERSION)
INSTRUCTION=('证据传输格式：$v4_table=1 表示无损列表示，columns 每项是原对象的字段路径，'
 'rows 按 columns 顺序给出每行值，constants 是每行共有的[字段路径,值]。'
 '将 constants 与每行合并即可还原观测，不得把列号当指标。'
 '@h编号是不透明证据ID，引用/查询使用收到的编号，工具参数仍按原schema填写。'
 '短ID以分号结束；双@@表示原文中的@。事实、单位、参考是否健康和角色约束保持。')
HASH=re.compile(r'(?<![a-zA-Z0-9])[0-9a-f]{64}(?![a-zA-Z0-9])')
ALIAS=re.compile(r'@h([0-9a-zA-Z]+);?')
MAX_DEPTH=40


def _flat(obj,prefix=(),depth=0):
    if depth>MAX_DEPTH:raise ValueError('wire nesting limit')
    out={}
    for k,v in obj.items():
        if not isinstance(k,str):raise ValueError('wire keys must be strings')
        path=prefix+(k,)
        if isinstance(v,dict) and v and '$v4_table' not in v and '$v4_literal' not in v:out.update(_flat(v,path,depth+1))
        else:out[path]=v
    return out


def encode_value(value,depth=0):
    if depth>MAX_DEPTH:raise ValueError('wire nesting limit')
    if isinstance(value,dict):
        out={k:encode_value(v,depth+1) for k,v in value.items()}
        if '$v4_table' in value or '$v4_literal' in value:return {'$v4_literal':out}
        return out
    if not isinstance(value,list):return value
    values=[encode_value(v,depth+1) for v in value]
    if len(value)<2 or not all(isinstance(v,dict) and v for v in value):return values
    # Original reserved tags stay literal; no ambiguity in table restoration.
    if any('$v4_table' in v or '$v4_literal' in v for v in value):return values
    rows=[_flat(v) for v in values];fields=list(rows[0])
    if any(set(row)!=set(fields) for row in rows[1:]):return values
    constants=[];columns=[]
    for path in fields:
        signature=dumps(rows[0][path])
        if all(dumps(row[path])==signature for row in rows[1:]):constants.append([list(path),rows[0][path]])
        else:columns.append(path)
    packed={'$v4_table':1,'columns':[list(p) for p in columns],
            'constants':constants,'rows':[[row[p] for p in columns] for row in rows]}
    return packed if len(dumps(packed))<len(dumps(values)) else values


def _put(obj,path,value):
    if not isinstance(path,list) or not path or any(not isinstance(k,str) for k in path):raise ValueError('invalid wire column path')
    for k in path[:-1]:
        item=obj.setdefault(k,{})
        if not isinstance(item,dict):raise ValueError('wire path conflict')
        obj=item
    if path[-1] in obj:raise ValueError('duplicate wire column')
    obj[path[-1]]=value


def decode_value(value,depth=0):
    if depth>MAX_DEPTH:raise ValueError('wire nesting limit')
    if isinstance(value,list):return [decode_value(v,depth+1) for v in value]
    if not isinstance(value,dict):return value
    if set(value)=={'$v4_literal'}:
        if not isinstance(value['$v4_literal'],dict):raise ValueError('invalid wire literal')
        return {k:decode_value(v,depth+1) for k,v in value['$v4_literal'].items()}
    if value.get('$v4_table')==1:
        if set(value)!={'$v4_table','columns','constants','rows'}:raise ValueError('invalid wire table fields')
        columns,constants,rows=(value[k] for k in ('columns','constants','rows'))
        if not all(isinstance(v,list) for v in (columns,constants,rows)):raise ValueError('invalid wire table')
        if len(rows)>100000 or len(columns)+len(constants)>10000:raise ValueError('wire table limit')
        result=[]
        for row in rows:
            if not isinstance(row,list) or len(row)!=len(columns):raise ValueError('wire row width')
            out={}
            for item in constants:
                if not isinstance(item,list) or len(item)!=2:raise ValueError('wire constant')
                _put(out,item[0],decode_value(item[1],depth+1))
            for path,item in zip(columns,row):_put(out,path,decode_value(item,depth+1))
            result.append(out)
        return result
    return {k:decode_value(v,depth+1) for k,v in value.items()}


class WireCodec:
    def __init__(self,request):
        hashes=set()
        def visit(v,depth=0):
            if depth>MAX_DEPTH:raise ValueError('wire nesting limit')
            if isinstance(v,str):
                hashes.update(HASH.findall(v))
                # JSON contents are transformed after decoding, including embedded
                # complete tool documents. Escaped whitespace changes hash boundaries.
                try:parsed=loads(v)
                except (ValueError,RecursionError):return
                if isinstance(parsed,(dict,list)) or (isinstance(parsed,str) and parsed!=v):visit(parsed,depth+1)
            elif isinstance(v,dict):
                for k,item in v.items():visit(k,depth+1);visit(item,depth+1)
            elif isinstance(v,list):
                for item in v:visit(item,depth+1)
        visit(request)
        self.aliases={v:'@h'+str(i)+';' for i,v in enumerate(sorted(hashes))}
        self.reverse={v:k for k,v in self.aliases.items()}
        self.contents={}
        self.message_contents=[]

    def encode_string(self,text):
        escaped=text.replace('@','@@')
        return HASH.sub(lambda m:self.aliases[m.group()],escaped)

    def decode_string(self,text):
        out=[];i=0
        while i<len(text):
            if text.startswith('@@',i):out.append('@');i+=2;continue
            if text.startswith('@h',i):
                match=ALIAS.match(text,i)
                key=match.group() if match is not None else None
                # Models sometimes omit the terminator in free prose. Resolve only an
                # exact known token; never infer a prefix, unknown ID, or literal @@.
                if key is not None and not key.endswith(';'):key+=';'
                if key not in self.reverse:raise ValueError('unknown wire reference')
                out.append(self.reverse[key]);i=match.end();continue
            out.append(text[i]);i+=1
        return ''.join(out)

    def transform(self,value,decode=False):
        f=self.decode_string if decode else self.encode_string
        if isinstance(value,str):return f(value)
        if isinstance(value,list):return [self.transform(v,decode) for v in value]
        if isinstance(value,dict):return {f(k):self.transform(v,decode) for k,v in value.items()}
        return value

    def encode_content(self,text):
        try:value=loads(text)
        except (ValueError,RecursionError):return self.encode_string(text)
        # Complete tool text contains another JSON document; partial chunks stay text.
        if isinstance(value,dict) and value.get('complete') is True and isinstance(value.get('text'),str):
            try:inner=loads(value['text'])
            except (ValueError,RecursionError):pass
            else:
                encoded=dumps(encode_value(self.transform(inner)))
                self.contents[encoded]=value['text']
                value=dict(value,text=encoded)
                # The embedded document was already aliased. Outer encode must not escape it again.
                other={k:v for k,v in value.items() if k!='text'}
                value=dict(self.transform(other),text=encoded)
                encoded=dumps(encode_value(value));self.contents[encoded]=text;return encoded
        encoded=dumps(encode_value(self.transform(value)));self.contents[encoded]=text;return encoded

    def decode_content(self,text,original=None):
        try:value=loads(text)
        except (ValueError,RecursionError):return self.decode_string(text)
        value=decode_value(value)
        if isinstance(value,dict) and value.get('complete') is True and isinstance(value.get('text'),str):
            inner=value['text']
            try:decoded=self.transform(decode_value(loads(inner)),True)
            except (ValueError,RecursionError):pass
            else:
                inner_original=self.contents.get(inner)
                if original is not None:
                    outer=loads(original)
                    if isinstance(outer,dict):inner_original=outer.get('text')
                if inner_original is not None:
                    if dumps(decoded)!=dumps(loads(inner_original)):raise ValueError('wire tool facts changed')
                    decoded=inner_original
                else:decoded=dumps(decoded)
                value=dict(value,text=decoded)
                other={k:v for k,v in value.items() if k!='text'}
                value=dict(self.transform(other,True),text=decoded)
                original=original if original is not None else self.contents.get(text)
                if original is not None:
                    if dumps(value)!=dumps(loads(original)):raise ValueError('wire facts changed')
                    return original
                return dumps(value)
        value=self.transform(value,True)
        original=original if original is not None else self.contents.get(text)
        if original is not None:
            if dumps(value)!=dumps(loads(original)):raise ValueError('wire facts changed')
            return original
        return dumps(value)

    def decode_response(self,message):
        result=dict(message)
        if isinstance(result.get('content'),str):result['content']=self.decode_string(result['content'])
        if result.get('tool_calls'):
            calls=[]
            for call in result['tool_calls']:
                function=dict(call['function']);function['arguments']=dumps(self.transform(loads(function['arguments']),True))
                calls.append(dict(call,function=function))
            result['tool_calls']=calls
        return result


def _pack_v1(request):
    codec=WireCodec(request);wire={k:v for k,v in request.items() if k not in ('messages','tools')}
    messages=[]
    codec.message_contents=[m.get('content') for m in request.get('messages',[])]
    for m in request.get('messages',[]):
        item=dict(m)
        if isinstance(item.get('content'),str):item['content']=codec.encode_content(item['content'])
        if item.get('tool_calls'):
            item['tool_calls']=codec.transform(item['tool_calls'])
        messages.append(item)
    if messages and messages[0]['role']=='system':messages[0]['content']+='\n'+INSTRUCTION
    elif messages:messages.insert(0,dict(role='system',content=INSTRUCTION))
    wire['messages']=messages;wire['tools']=codec.transform(request.get('tools',[]))
    return wire,codec


def _unpack_v1(wire,codec):
    result={k:v for k,v in wire.items() if k not in ('messages','tools')};messages=[]
    for m in wire.get('messages',[]):
        item=dict(m);text=item.get('content')
        if item['role']=='system' and text==INSTRUCTION:continue
        if isinstance(text,str):
            if item['role']=='system' and text.endswith('\n'+INSTRUCTION):text=text[:-len(INSTRUCTION)-1]
            original=codec.message_contents[len(messages)]
            item['content']=codec.decode_content(text,original)
        if item.get('tool_calls'):item['tool_calls']=codec.transform(item['tool_calls'],True)
        messages.append(item)
    result['messages']=messages;result['tools']=codec.transform(wire.get('tools',[]),True)
    return result


def request_stats(request,wire,codec):
    encoded=dumps(wire).encode('utf-8')
    return dict(version=getattr(codec,'version',VERSION),logical_bytes=len(dumps(request).encode('utf-8')),
                aliases=len(codec.aliases),request_bytes=len(encoded),
                request_sha256=hashlib.sha256(encoded).hexdigest())


# The v1 codec above remains byte-identical for historical recorded delivery.
# v2 is an explicit opt-in layer over complete encoded JSON documents only.
_REFERENCE_KEYS=frozenset(('$v4_fact_def','$v4_fact_ref','$v4_fact_literal'))
_REFERENCE_INSTRUCTION=('精确对象引用：{"$v4_fact_def":"fN","value":对象} 定义完整对象，'
 '后续 {"$v4_fact_ref":"fN"} 复用该对象全部字段，不是额外独立证据。'
 '先按出现顺序展开引用，再还原 $v4_table。{"$v4_fact_literal":对象} 是原文字面内容，不解释其引用标记。'
 '仅完整 JSON 文档使用对象引用；complete=false 的 text 仍是不完整原文片段，不得补全或引用未交付部分。'
 '单位、缺失、质量与来源保持；引用证据仍用原 @h ID，不用 fN。')


def _reference_literal(value):
    if isinstance(value,dict):
        if _REFERENCE_KEYS.intersection(value):return {'$v4_fact_literal':value}
        return {k:_reference_literal(v) for k,v in value.items()}
    if isinstance(value,list):return [_reference_literal(v) for v in value]
    return value


class _ReferencePool:
    def __init__(self,documents):
        self.counts={};self.labels={}
        for value in documents:self.count(value)

    def count(self,value):
        if not isinstance(value,(dict,list)):return
        if isinstance(value,dict) and '$v4_fact_literal' in value:return
        key=dumps(value)
        if len(key.encode('utf-8'))>=256:self.counts[key]=self.counts.get(key,0)+1
        # Existing column tables are atomic facts. Do not introduce reference
        # objects into their column paths or row layout.
        if isinstance(value,dict) and value.get('$v4_table')==1:return
        for item in value.values() if isinstance(value,dict) else value:self.count(item)

    def encode(self,value):
        if not isinstance(value,(dict,list)):return value
        if isinstance(value,dict) and '$v4_fact_literal' in value:return value
        key=dumps(value)
        if self.counts.get(key,0)>1:
            if key in self.labels:return {'$v4_fact_ref':self.labels[key]}
            label='f'+str(len(self.labels));self.labels[key]=label
            return {'$v4_fact_def':label,'value':self.children(value)}
        return self.children(value)

    def children(self,value):
        if isinstance(value,dict):
            if value.get('$v4_table')==1:return value
            return {k:self.encode(v) for k,v in value.items()}
        return [self.encode(v) for v in value]


def _used_references(value,counts):
    if isinstance(value,dict):
        if '$v4_fact_literal' in value:return
        if set(value)=={'$v4_fact_ref'}:
            label=value['$v4_fact_ref'];counts[label]=counts.get(label,0)+1;return
        for item in value.values():_used_references(item,counts)
    elif isinstance(value,list):
        for item in value:_used_references(item,counts)


def _prune_definitions(value,used):
    if isinstance(value,dict):
        if '$v4_fact_literal' in value:return value
        if set(value)=={'$v4_fact_def','value'} and not used.get(value['$v4_fact_def']):
            return _prune_definitions(value['value'],used)
        return {k:_prune_definitions(v,used) for k,v in value.items()}
    if isinstance(value,list):return [_prune_definitions(v,used) for v in value]
    return value


class _ReferenceDecoder:
    def __init__(self):self.values={};self.declared=set()

    def expand(self,value,depth=0):
        if depth>2*MAX_DEPTH+8:raise ValueError('wire reference nesting limit')
        if isinstance(value,list):return [self.expand(v,depth+1) for v in value]
        if not isinstance(value,dict):return value
        keys=set(value)
        if keys=={'$v4_fact_literal'}:
            if not isinstance(value['$v4_fact_literal'],dict):raise ValueError('invalid reference literal')
            return value['$v4_fact_literal']
        if keys=={'$v4_fact_ref'}:
            label=value['$v4_fact_ref']
            if not isinstance(label,str) or label not in self.values:raise ValueError('unknown or forward wire reference')
            # Each occurrence is restored independently before the v1 decoder.
            return loads(dumps(self.values[label]))
        if keys=={'$v4_fact_def','value'}:
            label=value['$v4_fact_def']
            if not isinstance(label,str) or re.fullmatch(r'f[0-9]+',label) is None or label in self.declared:
                raise ValueError('invalid or duplicate wire reference definition')
            self.declared.add(label)
            result=self.expand(value['value'],depth+1)
            self.values[label]=result
            return loads(dumps(result))
        if keys.intersection(_REFERENCE_KEYS):raise ValueError('invalid wire reference marker')
        return {k:self.expand(v,depth+1) for k,v in value.items()}


def _pack_reference_request(request,*,version=VERSION):
    if version not in SUPPORTED_VERSIONS:raise ValueError('unsupported evidence wire version')
    baseline,codec=_pack_v1(request);codec.version=version;codec.reference_mode=False
    if version==VERSION:return baseline,codec
    documents=[]
    for index,message in enumerate(baseline['messages']):
        text=message.get('content')
        if not isinstance(text,str):continue
        try:value=loads(text)
        except (ValueError,RecursionError):continue
        if not isinstance(value,(dict,list)):continue
        if message.get('role')=='tool' and isinstance(value,dict) and isinstance(value.get('text'),str):
            if value.get('complete') is False:continue
            if value.get('complete') is True:
                try:inner=loads(value['text'])
                except (ValueError,RecursionError):pass
                else:
                    if isinstance(inner,(dict,list)):
                        documents.append((index,'inner',_reference_literal(inner)));continue
        documents.append((index,'json',_reference_literal(value)))
    pool=_ReferencePool([v for _,_,v in documents])
    encoded=[pool.encode(v) for _,_,v in documents];used={}
    for value in encoded:_used_references(value,used)
    if not used:return baseline,codec
    encoded=[_prune_definitions(v,used) for v in encoded]
    wire=dict(baseline,messages=[dict(m) for m in baseline['messages']])
    for (index,kind,_),value in zip(documents,encoded):
        if kind=='inner':
            outer=loads(wire['messages'][index]['content']);outer['text']=dumps(value);text=dumps(outer)
        else:text=dumps(value)
        wire['messages'][index]['content']=text
    if wire['messages'] and wire['messages'][0]['role']=='system':
        wire['messages'][0]['content']+='\n'+_REFERENCE_INSTRUCTION
    if len(dumps(wire).encode('utf-8'))>=len(dumps(baseline).encode('utf-8')):return baseline,codec
    codec.reference_mode=True;codec.reference_documents=[(index,kind) for index,kind,_ in documents]
    return wire,codec


def _unpack_reference_request(wire,codec):
    if not getattr(codec,'reference_mode',False):return _unpack_v1(wire,codec)
    expanded=dict(wire,messages=[dict(m) for m in wire.get('messages',[])])
    if not expanded['messages'] or not expanded['messages'][0].get('content','').endswith('\n'+_REFERENCE_INSTRUCTION):
        raise ValueError('wire reference instruction changed')
    expanded['messages'][0]['content']=expanded['messages'][0]['content'][:-len(_REFERENCE_INSTRUCTION)-1]
    decoder=_ReferenceDecoder()
    for index,kind in codec.reference_documents:
        value=loads(expanded['messages'][index]['content'])
        if kind=='inner':value=dict(value,text=dumps(decoder.expand(loads(value['text']))))
        else:value=decoder.expand(value)
        expanded['messages'][index]['content']=dumps(value)
    return _unpack_v1(expanded,codec)

# v3 adds a lossless union-of-columns encoding for heterogeneous observations.
# v1 and v2 delivery remain unchanged so archived protocols remain replayable.
SHAPE_VERSION='v4-evidence-wire-3'
SUPPORTED_VERSIONS=(VERSION,REFERENCE_VERSION,SHAPE_VERSION)
_SPARSE_TAGS=frozenset(('$v4_sparse_table','$v4_sparse_literal','$v4_tree_table'))
_ATOMIC_TAGS=_SPARSE_TAGS|_REFERENCE_KEYS|frozenset(('$v4_table','$v4_literal'))
_SPARSE_INSTRUCTION=('异构观测表：$v4_sparse_table=1 的 columns 是字段路径，constants 是每行共有的[路径,值]。'
 '每行 absent 是该行缺少的 columns 零起始索引，values 按其余 columns 原顺序排列。'
 '先恢复异构表，再按既有说明展开精确对象引用和普通列表示。缺少字段不等于 null 或零；不增加或删除观测。'
 '$v4_sparse_literal 包裹的是原文字面对象，不解释该对象顶层标记。'
 '树状表 $v4_tree_table=1 的 schema 是字段树：整数叶子是 rows 的零起始列号，[=,编号,值] 是每行共享常量；按树路径还原字段。')

def _sparse_flat(obj,prefix=(),depth=0):
    if depth>MAX_DEPTH:raise ValueError('wire sparse nesting limit')
    out={}
    for key,value in obj.items():
        path=prefix+(key,)
        if isinstance(value,dict) and value and not _ATOMIC_TAGS.intersection(value):
            out.update(_sparse_flat(value,path,depth+1))
        else:out[path]=value
    return out


def _tree_encode(value):
    tree={}
    for index,path in enumerate(value['columns']):_put(tree,path,index)
    for index,(path,cell) in enumerate(value['constants']):_put(tree,path,['=',index,cell])
    packed={'$v4_tree_table':1,'schema':tree,'rows':value['rows']}
    return packed if len(dumps(packed))<len(dumps(value)) else value

def _tree_decode(value):
    if set(value)!={'$v4_tree_table','schema','rows'} or not isinstance(value['schema'],dict) or not isinstance(value['rows'],list):raise ValueError('invalid tree table')
    columns={};constants={}
    def visit(node,path=(),depth=0):
        if depth>MAX_DEPTH:raise ValueError('tree table nesting')
        if isinstance(node,dict):
            for key,child in node.items():visit(child,path+(key,),depth+1)
        elif type(node) is int and node>=0 and node not in columns:columns[node]=list(path)
        elif isinstance(node,list) and len(node)==3 and node[0]=='=' and type(node[1]) is int and node[1]>=0 and node[1] not in constants:constants[node[1]]=[list(path),node[2]]
        else:raise ValueError('invalid tree column')
    visit(value['schema'])
    if len(value['rows'])>100000 or len(columns)+len(constants)>10000 or set(columns)!=set(range(len(columns))) or set(constants)!=set(range(len(constants))):raise ValueError('tree column indices')
    if any(not isinstance(r,list) or len(r)!=len(columns) for r in value['rows']):raise ValueError('tree row width')
    return {'$v4_table':1,'columns':[columns[i] for i in range(len(columns))],'constants':[constants[i] for i in range(len(constants))],'rows':value['rows']}

def _sparse_encode(value,depth=0):
    if depth>MAX_DEPTH:raise ValueError('wire sparse nesting limit')
    if isinstance(value,dict):
        if value.get('$v4_table')==1 and set(value)=={'$v4_table','columns','constants','rows'}:return _tree_encode(value)
        # Literal wrappers and exact references remain opaque.
        if _ATOMIC_TAGS.difference(_SPARSE_TAGS).intersection(value):return value
        out={k:_sparse_encode(v,depth+1) for k,v in value.items()}
        return {'$v4_sparse_literal':out} if _SPARSE_TAGS.intersection(value) else out
    if not isinstance(value,list):return value
    values=[_sparse_encode(v,depth+1) for v in value]
    if len(values)<2 or not all(isinstance(v,dict) and not _ATOMIC_TAGS.intersection(v) for v in values):return values
    rows=[_sparse_flat(v) for v in values]
    fields=sorted(set().union(*(set(row) for row in rows)))
    if len(fields)>10000:return values
    common=set.intersection(*(set(row) for row in rows))
    constants=[];columns=[]
    for field in fields:
        if field in common and all(dumps(row[field])==dumps(rows[0][field]) for row in rows[1:]):
            constants.append([list(field),rows[0][field]])
        else:columns.append(field)
    packed={'$v4_sparse_table':1,'columns':[list(p) for p in columns],'constants':constants,
        'rows':[{'absent':[i for i,p in enumerate(columns) if p not in row],
                 'values':[row[p] for p in columns if p in row]} for row in rows]}
    return packed if len(dumps(packed))<len(dumps(values)) else values

def _sparse_decode(value,depth=0):
    if depth>MAX_DEPTH:raise ValueError('wire sparse nesting limit')
    if isinstance(value,list):return [_sparse_decode(v,depth+1) for v in value]
    if not isinstance(value,dict):return value
    if set(value)=={'$v4_sparse_literal'}:
        literal=value['$v4_sparse_literal']
        if not isinstance(literal,dict):raise ValueError('invalid sparse literal')
        return {k:_sparse_decode(v,depth+1) for k,v in literal.items()}
    if value.get('$v4_tree_table')==1:return _tree_decode(value)
    if value.get('$v4_sparse_table')==1:
        if set(value)!={'$v4_sparse_table','columns','constants','rows'}:raise ValueError('invalid sparse table')
        columns,constants,rows=(value[k] for k in ('columns','constants','rows'))
        if not all(isinstance(x,list) for x in (columns,constants,rows)) or len(rows)>100000 or len(columns)+len(constants)>10000:raise ValueError('sparse table limit')
        result=[]
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'absent','values'}:raise ValueError('invalid sparse row')
            absent,cells=row['absent'],row['values']
            if not isinstance(absent,list) or not isinstance(cells,list) or any(type(i) is not int or not 0<=i<len(columns) for i in absent) or absent!=sorted(set(absent)) or len(cells)!=len(columns)-len(absent):raise ValueError('invalid sparse absence/width')
            out={}
            for item in constants:
                if not isinstance(item,list) or len(item)!=2:raise ValueError('invalid sparse constant')
                _put(out,item[0],_sparse_decode(item[1],depth+1))
            omitted=set(absent);items=iter(cells)
            for i,path in enumerate(columns):
                if i not in omitted:_put(out,path,_sparse_decode(next(items),depth+1))
            result.append(out)
        return result
    if _ATOMIC_TAGS.difference(_SPARSE_TAGS).intersection(value):return value
    return {k:_sparse_decode(v,depth+1) for k,v in value.items()}

def pack_request(request,*,version=VERSION):
    if version!=SHAPE_VERSION:return _pack_reference_request(request,version=version)
    baseline,codec=_pack_reference_request(request,version=REFERENCE_VERSION)
    codec.version=version;codec.sparse_mode=False
    wire=dict(baseline,messages=[dict(m) for m in baseline.get('messages',[])])
    changed=[]
    for index,message in enumerate(baseline.get('messages',[])):
        text=message.get('content')
        if not isinstance(text,str):continue
        try:value=loads(text)
        except (ValueError,RecursionError):continue
        if not isinstance(value,(dict,list)):continue
        if message.get('role')=='tool' and isinstance(value,dict) and isinstance(value.get('text'),str):
            if value.get('complete') is False:continue
            if value.get('complete') is True:
                try:inner=loads(value['text'])
                except (ValueError,RecursionError):pass
                else:value=dict(value,text=dumps(_sparse_encode(inner)))
        encoded=dumps(_sparse_encode(value))
        wire['messages'][index]['content']=encoded
        changed.append((index,text))
    if not changed:return baseline,codec
    if wire['messages'] and wire['messages'][0]['role']=='system':
        wire['messages'][0]['content']+='\n'+_SPARSE_INSTRUCTION
    if len(dumps(wire).encode())>=len(dumps(baseline).encode()):return baseline,codec
    codec.sparse_mode=True;codec.sparse_original=changed
    return wire,codec

def unpack_request(wire,codec):
    if not getattr(codec,'sparse_mode',False):return _unpack_reference_request(wire,codec)
    expanded=dict(wire,messages=[dict(m) for m in wire.get('messages',[])])
    if not expanded['messages'][0].get('content','').endswith('\n'+_SPARSE_INSTRUCTION):raise ValueError('sparse instruction changed')
    expanded['messages'][0]['content']=expanded['messages'][0]['content'][:-len(_SPARSE_INSTRUCTION)-1]
    for index,original in codec.sparse_original:
        value=_sparse_decode(loads(expanded['messages'][index]['content']))
        before=loads(original)
        if expanded['messages'][index].get('role')=='tool' and isinstance(value,dict) and value.get('complete') is True and isinstance(value.get('text'),str):
            # complete=True also marks the last read_chunk fragment. A terminal
            # fragment need not be standalone JSON; packing leaves it literal.
            try:original_inner=loads(before['text'])
            except (ValueError,RecursionError):
                if value['text']!=before['text']:raise ValueError('sparse tool fragment changed')
            else:
                inner=_sparse_decode(loads(value['text']))
                if dumps(inner)!=dumps(original_inner):raise ValueError('sparse tool facts changed')
                value=dict(value,text=before['text'])
        if dumps(value)!=dumps(before):raise ValueError('sparse facts changed')
        expanded['messages'][index]['content']=original
    return _unpack_reference_request(expanded,codec)

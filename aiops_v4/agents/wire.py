"""Lossless evidence tables and per-request reference aliases at the HTTP boundary."""
import hashlib
import re
from .jsonio import dumps,loads

VERSION='v4-evidence-wire-1'
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
                if match is None or match.group() not in self.reverse:raise ValueError('unknown wire reference')
                out.append(self.reverse[match.group()]);i=match.end();continue
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


def pack_request(request):
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


def unpack_request(wire,codec):
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
    return dict(version=VERSION,logical_bytes=len(dumps(request).encode('utf-8')),
                aliases=len(codec.aliases),request_bytes=len(encoded),
                request_sha256=hashlib.sha256(encoded).hexdigest())

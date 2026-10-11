"""Lossless typed state deltas with layer-wide string dictionaries."""
import math
import struct
import zlib

MAGIC=b'V4D1'
MAX_BYTES=32*1024*1024


def _integer(value):
    result=bytearray()
    while value>=128:result.append((value&127)|128);value>>=7
    result.append(value);return result


def pack_delta(value,intern):
    out=bytearray()
    def put(item,depth=0):
        if depth>64:raise ValueError('state nesting exceeds limit')
        if item is None:out.append(0)
        elif type(item) is bool:out.append(2 if item else 1)
        elif type(item) is int:out.append(3);out.extend(_integer(item*2 if item>=0 else -item*2-1))
        elif type(item) is float:
            if not math.isfinite(item):raise ValueError('nonfinite state number')
            out.append(4);out.extend(struct.pack('<d',item))
        elif isinstance(item,str):out.append(5);out.extend(_integer(intern(item)))
        elif isinstance(item,list):
            out.append(6);out.extend(_integer(len(item)))
            for v in item:put(v,depth+1)
        elif isinstance(item,dict):
            out.append(7);out.extend(_integer(len(item)))
            for key in sorted(item):
                if not isinstance(key,str):raise ValueError('state keys must be strings')
                out.extend(_integer(intern(key)));put(item[key],depth+1)
        else:raise ValueError('unsupported state value')
        if len(out)>MAX_BYTES:raise ValueError('state payload too large')
    put(value)
    return MAGIC+zlib.compress(out,1)


def unpack_delta(blob,strings):
    if not blob.startswith(MAGIC):
        from aiops_v4.storage import load_json
        return load_json(blob)  # read-only compatibility with first prototype
    decoder=zlib.decompressobj()
    try:raw=decoder.decompress(blob[len(MAGIC):],MAX_BYTES+1)
    except zlib.error as exc:raise ValueError('invalid state payload') from exc
    if len(raw)>MAX_BYTES or not decoder.eof or decoder.unused_data:raise ValueError('invalid state payload')
    pos=0
    def integer():
        nonlocal pos
        value=shift=0
        while True:
            if pos>=len(raw) or shift>MAX_BYTES*8:raise ValueError('invalid state integer')
            byte=raw[pos];pos+=1;value|=(byte&127)<<shift
            if byte<128:return value
            shift+=7
    def string():
        try:return strings[integer()]
        except KeyError as exc:raise ValueError('unknown state string') from exc
    def get(depth=0):
        nonlocal pos
        if depth>64 or pos>=len(raw):raise ValueError('invalid state structure')
        tag=raw[pos];pos+=1
        if tag==0:return None
        if tag in (1,2):return tag==2
        if tag==3:
            n=integer();return n//2 if n%2==0 else -(n//2)-1
        if tag==4:
            if pos+8>len(raw):raise ValueError('invalid state float')
            n=struct.unpack_from('<d',raw,pos)[0];pos+=8
            if not math.isfinite(n):raise ValueError('nonfinite state number')
            return n
        if tag==5:return string()
        if tag==6:
            n=integer()
            if n>len(raw)-pos:raise ValueError('invalid state list')
            return [get(depth+1) for _ in range(n)]
        if tag==7:
            n=integer();result={}
            if n>len(raw)-pos:raise ValueError('invalid state object')
            for _ in range(n):
                key=string()
                if key in result:raise ValueError('duplicate state key')
                result[key]=get(depth+1)
            return result
        raise ValueError('invalid state type')
    value=get()
    if pos!=len(raw):raise ValueError('trailing state bytes')
    return value


class DeltaWriter:
    def __init__(self,db):
        self.db=db
        db.execute('CREATE TABLE IF NOT EXISTS strings(id INTEGER PRIMARY KEY,value TEXT UNIQUE)')
        self.strings={s:i for i,s in db.execute('SELECT id,value FROM strings')}

    def intern(self,value):
        if value not in self.strings:
            identifier=len(self.strings)+1;self.strings[value]=identifier
            self.db.execute('INSERT INTO strings VALUES (?,?)',(identifier,value))
        return self.strings[value]

    def pack(self,value):return pack_delta(value,self.intern)

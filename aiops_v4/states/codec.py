"""Lossless typed state deltas with layer-wide string dictionaries."""
import math
import struct
import zlib

MAGIC=b'V4D1'
RAW_MAGIC=b'V4R1'
MAX_BYTES=32*1024*1024


def _integer(value):
    result=bytearray()
    while value>=128:result.append((value&127)|128);value>>=7
    result.append(value);return result


def pack_delta(value,intern,*,compress=True):
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
    return MAGIC+zlib.compress(out,1) if compress else RAW_MAGIC+bytes(out)


def unpack_delta(blob,strings):
    if blob.startswith(RAW_MAGIC):
        raw=blob[len(RAW_MAGIC):]
        if len(raw)>MAX_BYTES:raise ValueError('invalid state payload')
    elif blob.startswith(MAGIC):
        decoder=zlib.decompressobj()
        try:raw=decoder.decompress(blob[len(MAGIC):],MAX_BYTES+1)
        except zlib.error as exc:raise ValueError('invalid state payload') from exc
        if len(raw)>MAX_BYTES or not decoder.eof or decoder.unused_data:raise ValueError('invalid state payload')
    else:
        from aiops_v4.storage import load_json
        return load_json(blob)  # read-only compatibility with first prototype
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
        self.refresh()

    def refresh(self):
        self.strings={s:i for i,s in self.db.execute('SELECT id,value FROM strings')}
        self.next_id=max(self.strings.values(),default=0)+1

    def intern(self,value):
        if value not in self.strings:
            identifier=self.next_id;self.strings[value]=identifier;self.next_id+=1
            self.db.execute('INSERT INTO strings VALUES (?,?)',(identifier,value))
        return self.strings[value]

    def pack(self,value):return pack_delta(value,self.intern)


STATE_MAGIC=b'V4S2'
STATE_RAW_MAGIC=b'V4R2'
STATUSES=('scored','blocked_quality','insufficient_reference','insufficient_feature_overlap')
STATUS_CODES={value:index for index,value in enumerate(STATUSES)}
_STATE_KEYS=('schema_version','reference_status','scoring_status','is_reference','feature_coverage',
    'time_coverage','stat_score','cluster_id','cluster_distance','cluster_reference_support',
    'cluster_support_basis','state_changed','unseen_categories','quality_signals','quality_flags',
    'overflowed_features','drivers','statistical_signals','cluster_signals','rule_signals','triggers',
    'candidate','raw_candidate')
_STATE_BITS={key:1<<index for index,key in enumerate(_STATE_KEYS)}
_BOOL_KEYS=('is_reference','state_changed','candidate','raw_candidate')
_NUMBER_KEYS=('feature_coverage','stat_score','cluster_id','cluster_distance','cluster_reference_support')
_EMPTY_KEYS=('unseen_categories','quality_signals','overflowed_features','statistical_signals',
             'cluster_signals','rule_signals','triggers')


def _same(left,right):
    """Equality used for omission must retain scalar type and float bits."""
    if type(left) is not type(right):return False
    if type(left) is float:return struct.pack('<d',left)==struct.pack('<d',right)
    if isinstance(left,list):return len(left)==len(right) and all(_same(a,b) for a,b in zip(left,right))
    if isinstance(left,dict):return left.keys()==right.keys() and all(_same(left[k],right[k]) for k in left)
    return left==right


def _driver(feature,deviation,vector):
    cell=vector['features'][feature]
    return dict(feature=feature,deviation=deviation,**{key:cell[key] for key in
        ('raw_value','unit','transform','semantic_status')})


def _derived(vector,reference_status):
    return dict(schema_version=1,reference_status=reference_status,
        cluster_support_basis='bounded_reference_sample',time_coverage=vector['quality']['time_coverage'],
        quality_flags=vector['quality']['flags'],**{key:[] for key in _EMPTY_KEYS})


def pack_state_delta(value,vector,reference_status,intern,*,compress=True):
    """Persist decisions once; omit only exactly reproducible canonical fields."""
    if not isinstance(value,dict):raise ValueError('state delta must be an object')
    presence=sum(1<<index for index,key in enumerate(_STATE_KEYS) if key in value)
    extra=dict(value);flags=0
    for index,key in enumerate(_BOOL_KEYS):
        if type(value.get(key)) is bool:
            if extra.pop(key):flags|=1<<index
    status=value.get('scoring_status')
    code=STATUS_CODES.get(status,-1) if isinstance(status,str) else -1
    if code>=0:extra.pop('scoring_status')
    numbers=[]
    for key in _NUMBER_KEYS:
        number=value.get(key)
        if number is None or type(number) in (int,float):
            numbers.append(number);extra.pop(key,None)
        else:numbers.append(None)
    for key,derived in _derived(vector,reference_status).items():
        if key in extra and _same(extra[key],derived):extra.pop(key)
    drivers=[]
    if isinstance(value.get('drivers',[]),list):
        for driver in value.get('drivers',[]):
            try:
                feature,deviation=driver['feature'],driver['deviation']
                if (not isinstance(feature,str) or not feature or type(deviation) not in (int,float) or
                    not math.isfinite(deviation) or not _same(driver,_driver(feature,deviation,vector))):break
                drivers.append([feature,deviation])
            except (KeyError,TypeError):break
        else:extra.pop('drivers',None)
        if 'drivers' in extra:drivers=[]
    payload=[presence,flags,code,*numbers,drivers,extra]
    return (STATE_MAGIC if compress else STATE_RAW_MAGIC)+pack_delta(payload,intern,compress=compress)[len(MAGIC):]


def _validate_presence(presence,flags,code,numbers_present,driver_count,extra_bits,check_overrides=True):
    implied=numbers_present|extra_bits
    for index,key in enumerate(_BOOL_KEYS):
        if flags&(1<<index):implied|=_STATE_BITS[key]
    if code>=0:implied|=_STATE_BITS['scoring_status']
    if driver_count:implied|=_STATE_BITS['drivers']
    if implied&~presence:raise ValueError('compact state presence disagrees with decisions')
    if check_overrides and presence&_STATE_BITS['scoring_status'] and code<0 and not extra_bits&_STATE_BITS['scoring_status']:
        raise ValueError('missing compact state scoring status')


def unpack_state_delta(blob,strings,vector,reference_status):
    if blob[:4] not in (STATE_MAGIC,STATE_RAW_MAGIC):return unpack_delta(blob,strings)
    value=unpack_delta((MAGIC if blob.startswith(STATE_MAGIC) else RAW_MAGIC)+blob[4:],strings)
    if not isinstance(value,list) or len(value)!=10:raise ValueError('invalid compact state structure')
    presence,flags,code,*tail=value
    if (type(presence) is not int or not 0<=presence<1<<len(_STATE_KEYS) or
        type(flags) is not int or not 0<=flags<1<<len(_BOOL_KEYS) or
        type(code) is not int or not -1<=code<len(STATUSES)):
        raise ValueError('invalid compact state decisions')
    numbers,drivers,extra=tail[:5],tail[5],tail[6]
    if any(number is not None and type(number) not in (int,float) for number in numbers):
        raise ValueError('invalid compact state numeric decision')
    if not isinstance(drivers,list) or not isinstance(extra,dict):raise ValueError('invalid compact state extras')
    extra_bits=sum(_STATE_BITS.get(key,0) for key in extra)
    numbers_present=sum(_STATE_BITS[key] for key,number in zip(_NUMBER_KEYS,numbers) if number is not None)
    _validate_presence(presence,flags,code,numbers_present,len(drivers),extra_bits)
    defaults=_derived(vector,reference_status)
    defaults.update({key:bool(flags&(1<<index)) for index,key in enumerate(_BOOL_KEYS)})
    defaults.update(zip(_NUMBER_KEYS,numbers))
    if code>=0:defaults['scoring_status']=STATUSES[code]
    try:
        if any(not isinstance(item,list) or len(item)!=2 or not isinstance(item[0],str) or not item[0] or
               type(item[1]) not in (int,float) or not math.isfinite(item[1]) for item in drivers):
            raise ValueError('invalid compact state drivers')
        defaults['drivers']=[_driver(feature,deviation,vector) for feature,deviation in drivers]
    except (KeyError,TypeError) as exc:raise ValueError('invalid compact state driver feature') from exc
    result={key:defaults[key] for index,key in enumerate(_STATE_KEYS) if presence&(1<<index) and key in defaults}
    result.update(extra)
    if any(key not in result for index,key in enumerate(_STATE_KEYS) if presence&(1<<index)):
        raise ValueError('missing compact state field')
    return result


def remap_delta(blob,identifiers,*,strings=None,feature_names=None,compress=True):
    """Remap tokens and validate compact records without building state objects.

    The worker dictionary and persisted model feature names let import validate
    driver names and known override presence with constant-time token lookups.
    """
    magic=blob[:4]
    if magic not in (MAGIC,STATE_MAGIC,RAW_MAGIC,STATE_RAW_MAGIC):raise ValueError('unsupported typed state payload')
    if feature_names is not None and strings is None:raise ValueError('feature names require worker strings')
    if magic in (RAW_MAGIC,STATE_RAW_MAGIC):
        raw=blob[4:]
        if len(raw)>MAX_BYTES:raise ValueError('invalid state payload')
    else:
        decoder=zlib.decompressobj()
        try:raw=decoder.decompress(blob[4:],MAX_BYTES+1)
        except zlib.error as exc:raise ValueError('invalid state payload') from exc
        if len(raw)>MAX_BYTES or not decoder.eof or decoder.unused_data:raise ValueError('invalid state payload')
    out=bytearray();pos=0;header=[0,0,-1];numbers_present=driver_count=extra_bits=extra_count=0
    def integer():
        nonlocal pos
        value=shift=0
        while True:
            if pos>=len(raw) or shift>MAX_BYTES*8:raise ValueError('invalid state integer')
            byte=raw[pos];pos+=1;value|=(byte&127)<<shift
            if byte<128:return value
            shift+=7
    def string(role=None):
        nonlocal extra_bits
        old=integer()
        try:new=identifiers[old]
        except KeyError as exc:raise ValueError('unknown state string') from exc
        if type(new) is not int or new<1:raise ValueError('invalid state string identifier')
        if strings is not None:
            try:name=strings[old]
            except KeyError as exc:raise ValueError('unknown worker state string') from exc
            if not isinstance(name,str):raise ValueError('invalid worker state string')
            if role=='feature' and (not name or feature_names is not None and name not in feature_names):
                raise ValueError('invalid compact state driver feature')
            if role=='extra':extra_bits|=_STATE_BITS.get(name,0)
        out.extend(_integer(new))
        return new
    def copy(depth=0,slot=None,role=None):
        nonlocal pos,numbers_present,driver_count,extra_count
        if depth>64 or pos>=len(raw):raise ValueError('invalid state structure')
        tag=raw[pos];pos+=1;out.append(tag)
        compact_root=magic in (STATE_MAGIC,STATE_RAW_MAGIC) and depth==0
        if compact_root and tag!=6:raise ValueError('invalid compact state structure')
        if slot is not None:
            allowed=(3,) if slot<3 else (0,3,4) if slot<8 else (6,) if slot==8 else (7,)
            if tag not in allowed:raise ValueError('invalid compact state decision slot')
            if 3<=slot<8 and tag!=0:numbers_present|=_STATE_BITS[_NUMBER_KEYS[slot-3]]
        if role=='driver' and tag!=6:raise ValueError('invalid compact state driver entry')
        if role=='feature' and tag!=5:raise ValueError('invalid compact state driver feature')
        if role=='deviation' and tag not in (3,4):raise ValueError('invalid compact state driver deviation')
        if tag in (0,1,2):pass
        elif tag==3:
            number=integer();out.extend(_integer(number))
            if slot is not None and slot<3:
                scalar=number//2 if number%2==0 else -(number//2)-1
                valid=(0<=scalar<1<<len(_STATE_KEYS) if slot==0 else
                       0<=scalar<1<<len(_BOOL_KEYS) if slot==1 else -1<=scalar<len(STATUSES))
                if not valid:raise ValueError('invalid compact state decisions')
                header[slot]=scalar
        elif tag==4:
            if pos+8>len(raw):raise ValueError('invalid state float')
            if not math.isfinite(struct.unpack_from('<d',raw,pos)[0]):raise ValueError('nonfinite state number')
            out.extend(raw[pos:pos+8]);pos+=8
        elif tag==5:string(role)
        elif tag in (6,7):
            count=integer()
            if compact_root and count!=10:raise ValueError('invalid compact state structure')
            if role=='driver' and count!=2:raise ValueError('invalid compact state driver entry')
            if slot==8:driver_count=count
            if slot==9:extra_count=count
            if count>len(raw)-pos:raise ValueError('invalid state container')
            out.extend(_integer(count));keys=set()
            for index in range(count):
                if tag==7:
                    key=string('extra' if slot==9 else None)
                    if key in keys:raise ValueError('duplicate state key')
                    keys.add(key)
                child_role='driver' if slot==8 else ('feature' if index==0 else 'deviation') if role=='driver' else None
                copy(depth+1,index if compact_root else None,child_role)
        else:raise ValueError('invalid state type')
        if len(out)>MAX_BYTES:raise ValueError('state payload too large')
    copy()
    if pos!=len(raw):raise ValueError('trailing state bytes')
    if magic in (STATE_MAGIC,STATE_RAW_MAGIC):
        _validate_presence(*header,numbers_present,driver_count,extra_bits,strings is not None or extra_count==0)
    return ((STATE_MAGIC if magic in (STATE_MAGIC,STATE_RAW_MAGIC) else MAGIC)+zlib.compress(out,1) if compress else
            (STATE_RAW_MAGIC if magic in (STATE_MAGIC,STATE_RAW_MAGIC) else RAW_MAGIC)+bytes(out))

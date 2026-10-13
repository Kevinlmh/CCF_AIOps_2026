
"""Bounded, lossless blocks of raw typed state decisions."""
from collections import OrderedDict
import struct
import zlib
from .codec import RAW_MAGIC,STATE_RAW_MAGIC,MAX_BYTES

MAGIC=b"V4B1"
MAX_ROWS=256
MAX_BLOCK_BYTES=32*1024*1024
TARGET_BYTES=1024*1024
_HEADER=struct.Struct("<4sHI")
_UINT=struct.Struct("<I")


def pack_block(payloads):
    payloads=tuple(payloads)
    if not 1<=len(payloads)<=MAX_ROWS:raise ValueError("invalid state block row count")
    offsets=[0]
    for payload in payloads:
        if not isinstance(payload,bytes) or payload[:4] not in (RAW_MAGIC,STATE_RAW_MAGIC):
            raise ValueError("state block requires raw typed decisions")
        if len(payload)>MAX_BYTES:raise ValueError("state block record exceeds limit")
        offsets.append(offsets[-1]+len(payload))
    size=4*len(offsets)+offsets[-1]
    if size>MAX_BLOCK_BYTES:raise ValueError("state block exceeds byte limit")
    raw=b"".join(_UINT.pack(offset) for offset in offsets)+b"".join(payloads)
    return _HEADER.pack(MAGIC,len(payloads),size)+zlib.compress(raw,1)


def unpack_block(blob):
    if not isinstance(blob,bytes) or len(blob)<_HEADER.size or len(blob)>MAX_BLOCK_BYTES+65536:
        raise ValueError("invalid state block payload")
    magic,count,size=_HEADER.unpack_from(blob)
    if magic!=MAGIC or not 1<=count<=MAX_ROWS or not 4*(count+1)<=size<=MAX_BLOCK_BYTES:
        raise ValueError("invalid state block header")
    decoder=zlib.decompressobj()
    try:raw=decoder.decompress(blob[_HEADER.size:],size+1)
    except zlib.error as error:raise ValueError("invalid state block compression") from error
    if len(raw)!=size or not decoder.eof or decoder.unused_data:
        raise ValueError("invalid state block compression")
    offset_size=4*(count+1);data_size=size-offset_size
    offsets=struct.unpack_from("<"+"I"*(count+1),raw)
    if offsets[0]!=0 or offsets[-1]!=data_size or any(a>=b for a,b in zip(offsets,offsets[1:])):
        raise ValueError("invalid state block offsets")
    result=tuple(raw[offset_size+a:offset_size+b] for a,b in zip(offsets,offsets[1:]))
    if any(payload[:4] not in (RAW_MAGIC,STATE_RAW_MAGIC) for payload in result):
        raise ValueError("invalid state block record")
    return result


class BlockReader:
    def __init__(self,db,*,cache_bytes=8*1024*1024,cache_blocks=8):
        if any(type(v) is not int or v<1 for v in (cache_bytes,cache_blocks)):
            raise ValueError("positive state block cache limits required")
        self.db=db;self.cache=OrderedDict();self.cache_bytes=cache_bytes;self.cache_blocks=cache_blocks;self.retained_bytes=0

    def block(self,identifier):
        if identifier in self.cache:
            self.cache.move_to_end(identifier);return self.cache[identifier]
        row=self.db.execute("SELECT count,payload FROM state_blocks WHERE id=?",(identifier,)).fetchone()
        if row is None:raise ValueError("unknown state block")
        records=unpack_block(row[1])
        if len(records)!=row[0]:raise ValueError("state block count mismatch")
        size=sum(len(payload) for payload in records)+len(records)*64
        if size<=self.cache_bytes:
            while self.cache and (self.retained_bytes+size>self.cache_bytes or len(self.cache)>=self.cache_blocks):
                _,old=self.cache.popitem(last=False);self.retained_bytes-=sum(len(payload) for payload in old)+len(old)*64
            self.cache[identifier]=records;self.retained_bytes+=size
        return records

    def get(self,identifier,position):
        records=self.block(identifier)
        if type(position) is not int or not 0<=position<len(records):raise ValueError("invalid state block position")
        return records[position]


def batches(records,*,max_rows=MAX_ROWS,max_bytes=TARGET_BYTES):
    if type(max_rows) is not int or not 1<=max_rows<=MAX_ROWS or type(max_bytes) is not int or not 1<=max_bytes<MAX_BLOCK_BYTES:
        raise ValueError("invalid state block batch limits")
    page=[];size=0
    for record in records:
        payload=record[-1]
        if not isinstance(payload,bytes) or len(payload)+8>MAX_BLOCK_BYTES:
            raise ValueError("invalid state block record size")
        if page and (len(page)>=max_rows or size+len(payload)>max_bytes):
            yield page;page=[];size=0
        page.append(record);size+=len(payload)
    if page:yield page

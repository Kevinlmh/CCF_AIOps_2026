import json
import os
from importlib import import_module, util

import pytest

from aiops_v4.data.discovery import discover_sources
from aiops_v4.data.reader import RecordReader


def api():
    assert util.find_spec('aiops_v4.data.position'), 'byte-position reader not implemented'
    return import_module('aiops_v4.data.position')


@pytest.mark.parametrize('newline', [b'\n', b'\r\n', b'\r'])
def test_positions_preserve_bom_blanks_multiline_unicode_and_raw_identity(tmp_path, newline):
    path = tmp_path/'xian'/'frr_syslog_events.csv';path.parent.mkdir()
    body = newline.join([b'\xef\xbb\xbf', b'event_time,hostname,message',
        '2026-09-17T04:00:00Z,br1,"第一行'.encode(), '第二行"'.encode(), b'',
        b'2026-09-17T04:01:00Z,br1,"last"', b''])
    path.write_bytes(body)
    file = discover_sources(tmp_path)[0]
    with RecordReader(file, 'stage1') as old: expected = list(old)
    with api().PositionReader(file, 'stage1') as reader:
        positioned = list(reader)
        manifest = reader.manifest()
    assert [p.record(reader).to_dict() for p in positioned] == [r.to_dict() for r in expected]
    assert [(p.line_start,p.line_end,p.record_index) for p in positioned] == [(3,4,1),(6,6,2)]
    for p,r in zip(positioned,expected):
        anchor = p.anchor(reader)
        assert api().record_at(tmp_path,'stage1',manifest,anchor).to_dict() == r.to_dict()
        assert body[anchor['byte_start']:anchor['byte_start']+anchor['byte_length']].decode().startswith('2026')
    import hashlib
    assert manifest['raw_bytes_sha256'] == hashlib.sha256(body).hexdigest()
    assert manifest['complete'] is True


def test_saved_boundary_resumes_without_duplicate_records_and_prefix_hash_is_scoped(tmp_path):
    path=tmp_path/'node_metrics.csv'
    path.write_text('timestamp,node,cpu_usage\n2026-09-17T04:00:00Z,xian-br-1,0\n\n2026-09-17T04:01:00Z,xian-br-1,1\n')
    file=discover_sources(tmp_path)[0]
    with api().PositionReader(file,'stage1') as reader:
        first=next(iter(reader));saved=reader.checkpoint()
    with api().PositionReader(file,'stage1',checkpoint=saved) as continued:
        rest=list(continued);manifest=continued.manifest()
    assert [r.record_index for r in rest] == [2]
    assert manifest['rows_scanned'] == 2 and manifest['complete']
    with api().PositionReader(file,'stage1',max_rows=1) as limited:
        rows=list(limited);prefix=limited.manifest()
    assert len(rows)==1 and prefix['complete'] is False
    assert prefix['byte_range_end']==first.byte_start+first.byte_length
    assert prefix['raw_bytes_sha256'] != manifest['raw_bytes_sha256']


@pytest.mark.parametrize('preserve_mtime', [False, True])
def test_direct_read_rejects_changed_input_unregistered_span_and_escape(tmp_path, preserve_mtime):
    path=tmp_path/'node_metrics.csv';path.write_text('timestamp,node\n2026-09-17T04:00:00Z,xian-br-1\n')
    file=discover_sources(tmp_path)[0]
    with api().PositionReader(file,'stage1') as reader:
        row=next(iter(reader));anchor=row.anchor(reader);manifest=reader.manifest()
    with pytest.raises(ValueError): api().record_at(tmp_path,'stage1',dict(manifest,path='../node_metrics.csv'),anchor)
    with pytest.raises(ValueError): api().record_at(tmp_path,'stage1',manifest,dict(anchor,byte_start=0))
    path.write_text('timestamp,node\n2026-09-17T04:00:00Z,xian-br-2\n')
    # Fast same-size writes can share a filesystem timestamp. Exercise both
    # the metadata check and the content-ID guard without relying on clock ticks.
    mtime=manifest['identity']['mtime_ns']+(0 if preserve_mtime else 1_000_000_000)
    os.utime(path,ns=(path.stat().st_atime_ns,mtime))
    assert (api().file_identity(path)==manifest['identity']) is preserve_mtime
    error='content ID mismatch' if preserve_mtime else 'changed'
    with pytest.raises(ValueError,match=error): api().record_at(tmp_path,'stage1',manifest,anchor)


def test_duplicate_header_and_malformed_quotes_never_publish_valid_ranges(tmp_path):
    path=tmp_path/'node_metrics.csv';path.write_text('timestamp,node,node\n')
    file=discover_sources(tmp_path)[0]
    with pytest.raises(ValueError,match='duplicate'):
        with api().PositionReader(file,'stage1') as reader: list(reader)
    path.write_text('timestamp,node\n"unclosed,xian-br-1\n')
    with pytest.raises(ValueError,match='CSV'):
        with api().PositionReader(file,'stage1') as reader: list(reader)

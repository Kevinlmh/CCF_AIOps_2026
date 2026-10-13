"""Compact decisions retain exact state fields without rescoring observations."""
import copy
import sqlite3
import struct
import pytest
from aiops_v4.states import codec


def record():
    vector=dict(quality=dict(time_coverage=0.5,flags=['counter_gap']),features={
        'a::mean':dict(raw_value=-0.0,unit='ratio',transform='identity',semantic_status='inferred')})
    delta=dict(schema_version=1,reference_status='ready',scoring_status='scored',is_reference=False,
        feature_coverage=1.0,time_coverage=0.5,stat_score=-0.0,cluster_id=0,cluster_distance=0.0,
        cluster_reference_support=1.0,cluster_support_basis='bounded_reference_sample',state_changed=False,
        unseen_categories=[],quality_signals=[],quality_flags=['counter_gap'],overflowed_features=[],
        drivers=[dict(feature='a::mean',deviation=0.0,raw_value=-0.0,unit='ratio',transform='identity',semantic_status='inferred')],
        statistical_signals=[],cluster_signals=[],rule_signals=[],triggers=[],candidate=False,
        raw_candidate=True,suppression_reason='small_effect',unknown=dict(nested=[None,False,0,0.0,'自定义']))
    return vector,delta


def pack(delta,vector):
    assert callable(getattr(codec,'pack_state_delta',None)), 'compact state decision writer is missing'
    strings={}
    def intern(value):
        if value not in strings:strings[value]=len(strings)+1
        return strings[value]
    blob=codec.pack_state_delta(delta,vector,'ready',intern)
    return blob,{identifier:value for value,identifier in strings.items()}


def test_compact_decisions_reconstruct_exact_scores_flags_drivers_and_unknown_extras():
    vector,delta=record();before=copy.deepcopy((vector,delta))
    blob,strings=pack(delta,vector)
    restored=codec.unpack_state_delta(blob,strings,vector,'ready')
    assert restored==delta
    assert struct.pack('<d',restored['stat_score'])==struct.pack('<d',-0.0)
    assert struct.pack('<d',restored['drivers'][0]['raw_value'])==struct.pack('<d',-0.0)
    assert (vector,delta)==before
    with sqlite3.connect(':memory:') as db:
        old=codec.DeltaWriter(db).pack(delta)
    assert len(blob)<len(old)*0.8


def test_compact_payload_preserves_missing_none_zero_and_noncanonical_shared_metadata():
    vector,delta=record()
    for key in ('is_reference','time_coverage','quality_flags','cluster_reference_support','drivers','raw_candidate','unseen_categories'):
        delta.pop(key)
    delta.update(schema_version=7,reference_status='custom',cluster_support_basis='custom_basis',
                 feature_coverage=0,stat_score=None,cluster_id=None,cluster_distance=None,extra_empty=[])
    blob,strings=pack(delta,vector)
    restored=codec.unpack_state_delta(blob,strings,vector,'ready')
    assert restored==delta
    assert type(restored['feature_coverage']) is int
    assert 'raw_candidate' not in restored and 'drivers' not in restored


def test_driver_fallback_retains_fields_that_differ_from_current_vector():
    vector,delta=record()
    delta['drivers'][0].update(raw_value=99.0,unit='custom',detail={'note':'preserved'})
    delta['quality_flags']=[];delta['time_coverage']=None
    blob,strings=pack(delta,vector)
    assert codec.unpack_state_delta(blob,strings,vector,'ready')==delta


def test_typed_payload_remap_uses_parent_dictionary_and_rejects_missing_tokens():
    vector,delta=record();blob,strings=pack(delta,vector)
    parent={identifier+130:value for identifier,value in strings.items()}
    mapping={identifier:identifier+130 for identifier in strings}
    assert callable(getattr(codec,'remap_delta',None)), 'binary dictionary remapping is missing'
    remapped=codec.remap_delta(blob,mapping)
    assert codec.unpack_state_delta(remapped,parent,vector,'ready')==delta
    with pytest.raises(ValueError,match='string'):
        codec.remap_delta(blob,{})
    with sqlite3.connect(':memory:') as db:
        legacy=codec.DeltaWriter(db).pack(delta)
        legacy_strings={i:s for i,s in db.execute('SELECT id,value FROM strings')}
    assert codec.unpack_state_delta(legacy,legacy_strings,vector,'ready')==delta


@pytest.mark.parametrize('bad',[b'V4S2',b'V4S2bad',b'V4S2'+b'\x00'*20])
def test_compact_payload_rejects_truncation_and_invalid_typed_structure(bad):
    vector,_=record()
    assert callable(getattr(codec,'unpack_state_delta',None)), 'compact state reader is missing'
    with pytest.raises(ValueError):codec.unpack_state_delta(bad,{},vector,'ready')


@pytest.mark.parametrize('drivers',[None,{},'unknown'])
def test_nonstandard_driver_values_remain_lossless_overrides(drivers):
    vector,delta=record();delta['drivers']=drivers
    blob,strings=pack(delta,vector)
    assert codec.unpack_state_delta(blob,strings,vector,'ready')==delta


@pytest.mark.parametrize('raw',[
    [],[0,0,-1,None,None,None,None,None,[],{}],
    [-1,0,0,None,None,None,None,None,[],{}],
    [1<<30,0,0,None,None,None,None,None,[],{}],
    [0,32,0,None,None,None,None,None,[],{}],
    [0,0,99,None,None,None,None,None,[],{}],
    [0,0,0,None,None,None,None,None,'bad',{}]])
def test_invalid_compact_bitmasks_and_structure_are_rejected(raw):
    vector,_=record();strings={}
    blob=codec.STATE_MAGIC+codec.pack_delta(raw,lambda value:1)[4:]
    # A well-formed empty state has all fields absent and needs no decisions.
    if len(raw)==10 and raw[0:3]==[0,0,-1] and isinstance(raw[8],list):
        assert codec.unpack_state_delta(blob,strings,vector,'ready')=={}
    else:
        with pytest.raises(ValueError):codec.unpack_state_delta(blob,strings,vector,'ready')


@pytest.mark.parametrize('blob',[b'V4S2',b'V4S2bad',b'nope',b'V4D1'+b'\x00'*4])
def test_remap_rejects_corrupt_or_unsupported_frames(blob):
    with pytest.raises(ValueError):codec.remap_delta(blob,{})



def test_dictionary_refresh_after_rollback_never_returns_an_uncommitted_token():
    with sqlite3.connect(':memory:') as db:
        writer=codec.DeltaWriter(db);writer.pack({'committed':'first'});db.commit()
        writer.pack({'rolled_back':'gone'});db.rollback()
        assert callable(getattr(writer,'refresh',None)), 'dictionary rollback refresh is missing'
        writer.refresh();blob=writer.pack({'resumed':'second'})
        strings={i:s for i,s in db.execute('SELECT id,value FROM strings')}
        assert codec.unpack_delta(blob,strings)=={'resumed':'second'}
        assert 'gone' not in strings.values() and 'rolled_back' not in strings.values()



def test_corrupt_numeric_decision_slot_is_rejected_before_reconstruction():
    vector,_=record()
    malformed=[1<<4,0,-1,[],None,None,None,None,[],{}]
    blob=codec.STATE_MAGIC+codec.pack_delta(malformed,lambda value:1)[4:]
    with pytest.raises(ValueError,match='decision'):codec.unpack_state_delta(blob,{},vector,'ready')



@pytest.mark.parametrize('malformed',[
    [],[1<<30,0,-1,None,None,None,None,None,[],{}],
    [0,0,-1,[],None,None,None,None,[],{}]])
def test_remap_rejects_malformed_compact_header_before_parent_import(malformed):
    blob=codec.STATE_MAGIC+codec.pack_delta(malformed,lambda value:1)[4:]
    with pytest.raises(ValueError):codec.remap_delta(blob,{})



def compact_frame(raw):
    strings={}
    def intern(value):
        if value not in strings:strings[value]=len(strings)+1
        return strings[value]
    blob=codec.STATE_MAGIC+codec.pack_delta(raw,intern)[4:]
    return blob,{identifier:identifier+100 for identifier in strings.values()},{identifier:value for value,identifier in strings.items()}


@pytest.mark.parametrize('drivers',[
    [1],[[]],[['a::mean']],[['a::mean',0.0,1]],[[0,0.0]],[[None,0.0]],
    [['a::mean',None]],[['a::mean',True]],[['a::mean','bad']]])
def test_malformed_compact_driver_entries_are_rejected_before_import(drivers):
    raw=[1<<16,0,-1,None,None,None,None,None,drivers,{}]
    blob,mapping,strings=compact_frame(raw)
    with pytest.raises(ValueError):codec.remap_delta(blob,mapping)
    vector,_=record()
    with pytest.raises(ValueError):codec.unpack_state_delta(blob,strings,vector,'ready')


@pytest.mark.parametrize('raw',[
    [0,1,-1,None,None,None,None,None,[],{}],
    [0,0,0,None,None,None,None,None,[],{}],
    [0,0,-1,1.0,None,None,None,None,[],{}],
    [0,0,-1,None,None,None,None,None,[['a::mean',0.0]],{}]])
def test_compact_presence_mask_cannot_hide_active_decisions(raw):
    blob,mapping,strings=compact_frame(raw)
    with pytest.raises(ValueError):codec.remap_delta(blob,mapping)
    vector,_=record()
    with pytest.raises(ValueError):codec.unpack_state_delta(blob,strings,vector,'ready')


def test_noncanonical_driver_deviation_is_kept_as_sparse_override():
    vector,delta=record();delta['drivers'][0]['deviation']='unknown'
    blob,strings=pack(delta,vector)
    assert codec.unpack_state_delta(blob,strings,vector,'ready')==delta
    # The worker can retain this unknown detail, but compact pairs stay numeric.
    raw=codec.unpack_delta(codec.MAGIC+blob[4:],strings)
    assert raw[8]==[] and raw[9]['drivers']==delta['drivers']



@pytest.mark.parametrize('name',['','missing::mean'])
def test_import_checks_driver_name_against_persisted_model_features(name):
    blob,mapping,strings=compact_frame([1<<16,0,-1,None,None,None,None,None,[[name,0.0]],{}])
    with pytest.raises(ValueError,match='feature'):
        codec.remap_delta(blob,mapping,strings=strings,feature_names={'a::mean':{}})


def test_known_sparse_override_cannot_add_a_field_missing_from_presence_mask():
    raw=[0,0,-1,None,None,None,None,None,[],{'stat_score':None}]
    blob,mapping,strings=compact_frame(raw)
    with pytest.raises(ValueError,match='presence'):codec.remap_delta(blob,mapping,strings=strings)
    vector,_=record()
    with pytest.raises(ValueError,match='presence'):codec.unpack_state_delta(blob,strings,vector,'ready')


def test_missing_status_override_is_rejected_at_import_when_status_code_is_unknown():
    blob,mapping,strings=compact_frame([1<<2,0,-1,None,None,None,None,None,[],{}])
    with pytest.raises(ValueError,match='status'):codec.remap_delta(blob,mapping)
    with pytest.raises(ValueError,match='status'):codec.remap_delta(blob,mapping,strings=strings)


def test_import_remaps_valid_drivers_and_unknown_overrides_with_shared_dictionary():
    vector,delta=record();blob,strings=pack(delta,vector)
    mapping={old:old+128 for old in strings};parent={old+128:name for old,name in strings.items()}
    remapped=codec.remap_delta(blob,mapping,strings=strings,feature_names={'a::mean':{}})
    assert codec.unpack_state_delta(remapped,parent,vector,'ready')==delta

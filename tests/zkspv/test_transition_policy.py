"""Independent schema reader for complete journals and native-source provenance."""
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
HERE=ROOT/'rust/zkspv/transition-policy'

def cases():
    data=json.loads((ROOT/'web3/test/fixtures/transition-policy-v1.json').read_text())
    assert data['authentication'].startswith('NONE')
    return data['cases']

def decode(raw):
    schema=json.loads((HERE/'journal-schema.json').read_text())
    assert len(schema['words'])==32 and len(raw)==1024
    out={}
    for i,(name,kind) in enumerate(schema['words']):
        word=raw[i*32:(i+1)*32]
        if kind=='bytes32': value=word
        else:
            value=int.from_bytes(word,'big')
            width={'address':160,'bool':1}.get(kind)
            if width is None: width=int(kind.removeprefix('uint'))
            assert value<2**width,name
        out[name]=value
    for field in ['domain','bitcoin_network','time_domain']:
        assert out[field]==schema[field].encode().ljust(32,b'\0')
    return out

def test_all_shared_journals_match_independent_schema_and_count_semantics():
    assert len(cases())==6
    for case in cases():
        d=decode(bytes.fromhex(case['journal'][2:]))
        assert d['evm_chain_id']==31337 and d['state_address']==int('11'*20,16)
        assert d['anchor_height']==32256 and d['next_height']>=32256
        assert 0<d['new_header_count']<=d['processed_count']<=d['submitted_count']<=64
        assert d['new_header_count']==case['new_headers'] and d['processed_count']==case['processed']
        assert d['maximum_timestamp']==case['maximum'] and d['maximum_timestamp']>=d['last_new_timestamp']
        assert d['next_tip_time']==case['tip_time'] and bool(d['halted'])==case['halted']
        assert (d['next_work']>d['previous_work'])==case['useful']
        assert d['previous_state_commitment']!=d['next_state_commitment']
        if d['halted']:
            assert d['halt_position_one_based']==d['processed_count']==case['halt_position']
            assert d['halt_old_head']==d['next_head'] and d['halt_depth']==101
        else:
            assert d['processed_count']==d['submitted_count'] and d['halt_position_one_based']==0
            assert d['halt_depth']==0
            assert d['halt_old_head']==d['halt_candidate']==d['halt_lca']==bytes(32)

def test_canonical_tip_time_is_not_replaced_by_maximum_or_halt_candidate():
    fixtures={c['name']:decode(bytes.fromhex(c['journal'][2:])) for c in cases()}
    for name in ['nonmonotonic','losing_branch','halt_prefix']:
        d=fixtures[name]; assert d['maximum_timestamp']>d['next_tip_time']
    side=fixtures['losing_branch']
    for suffix in ['head','height','work','tip_time','canonical_root']:
        assert side['previous_'+suffix]==side['next_'+suffix]
    assert fixtures['shorter_heavier']['next_height']<fixtures['shorter_heavier']['previous_height']

def test_exact_upstream_dependencies_and_unchanged_native_adapter_are_used():
    spec=importlib.util.spec_from_file_location('transition_qualification_test',HERE/'qualify.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert len(module.dependencies())==16
    _,original,corrected=module.prepare.checked_sources()
    source=module.native_source(original,corrected)
    assert (HERE/'native_tests.rs').read_text() in source
    assert str(HERE/'src/transition.rs') in source
    for name in ['checkpoint-policy','fork-policy','time-policy']:
        assert (HERE.parent/name/'native_tests.rs').read_text() in source

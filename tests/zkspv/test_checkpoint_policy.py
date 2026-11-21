"""Independent checkpoint commitment encoding and qualification provenance."""
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
HERE=ROOT/'rust/zkspv/checkpoint-policy'

def load(name):
    spec=importlib.util.spec_from_file_location('checkpoint_'+name,HERE/(name+'.py'))
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

def digest(domain,*parts):
    message=b'SATOSHIFI_FORK_V1\0'+len(domain).to_bytes(4,'big')+domain
    return hashlib.sha256(message+b''.join(len(p).to_bytes(8,'big')+p for p in parts)).digest()

def test_shared_checkpoints_match_independent_wire_and_commitment_encoding():
    data=json.loads((ROOT/'web3/test/fixtures/checkpoint-policy-v1.json').read_text())
    assert data['authentication'].startswith('NONE')
    assert [c['height'] for c in data['checkpoints']]==[32256,34272,36288]
    external=(ROOT/'web3/test/fixtures/mainnet/expected-32246-36288.bin').read_bytes()
    for case in data['checkpoints']:
        raw=bytes.fromhex(case['raw'][2:]); height=case['height']; work=bytes.fromhex(case['work'][2:])
        prior=[bytes.fromhex(h[2:]) for h in case['prior']]
        assert len(prior)==10 and all(len(h)==80 for h in prior+[raw])
        hashes=[hashlib.sha256(hashlib.sha256(h).digest()).digest() for h in prior+[raw]]
        for i,h in enumerate(prior[1:]+[raw]): assert h[4:36]==hashes[i]
        offset=(height-32246)*68
        assert hashes[-1]==external[offset:offset+32][::-1]==bytes.fromhex(case['hash'][2:])
        assert work==external[offset+32:offset+64]
        times=[0]*11
        for i,h in enumerate(prior+[raw]): times[(height-10+i)%11]=int.from_bytes(h[68:72],'little')
        assert times==case['timestamps']
        bits=int.from_bytes(raw[72:76],'little'); epoch_time=int.from_bytes(raw[68:72],'little')
        assert (bits,epoch_time)==(case['bits'],case['epoch_time'])
        context=digest(b'citrea-LatestDaState-v1',hashes[-1],height.to_bytes(8,'big'),work,
            bits.to_bytes(4,'big'),epoch_time.to_bytes(4,'big'),b''.join(t.to_bytes(4,'big') for t in times))
        descriptor=digest(b'mainnet-checkpoint-v1',b'bitcoin-mainnet',raw,height.to_bytes(8,'big'),work,b''.join(prior))
        assert context.hex()==case['context_commitment'][2:]
        assert descriptor.hex()==case['descriptor_commitment'][2:]

def test_compact_guard_is_copied_verbatim_from_hash_checked_qualified_source():
    generator=load('generate_compact'); source,manifest=generator.generated()
    assert source==(HERE/'src/compact.rs').read_text()
    assert manifest==(HERE/'compact-source.json').read_text()
    pin=json.loads(manifest)
    assert pin['corrected_verifier_sha256']=='6032160ce35ea5cd043ad69600bde95fb4d5d921d00d6be3b561d5975832cfdc'

def test_dependencies_and_actual_native_integration_are_pinned():
    qualification=load('qualify')
    assert len(qualification.dependencies())==16
    _,original,corrected=qualification.prepare.checked_sources()
    native=qualification.native_source(original,corrected)
    assert (HERE/'native_tests.rs').read_text() in native
    assert str(HERE/'src/checkpoint.rs') in native
    assert str(HERE/'src/compact.rs') in native
    assert (HERE.parent/'fork-policy/native_tests.rs').read_text() in native
    assert (HERE.parent/'time-policy/native_tests.rs').read_text() in native

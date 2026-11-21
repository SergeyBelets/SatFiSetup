"""Independent V2 index/wire/state encoding checks, not a second Bitcoin core."""
import hashlib,importlib.util,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
HERE=ROOT/'rust/zkspv/resume-policy'
def h(domain,*parts):
    return hashlib.sha256(b'SATOSHIFI_FORK_V1\0'+len(domain).to_bytes(4,'big')+domain+b''.join(len(p).to_bytes(8,'big')+p for p in parts)).digest()
def unhex(value):return bytes.fromhex(value[2:])
def test_wire_opening_and_index_root_match_independent_encoding():
    fixture=json.loads((HERE/'fixtures/index-v2.json').read_text());assert fixture['authentication'].startswith('NONE')
    assert len(fixture['cases'])==2
    for case in fixture['cases']:
        wire=unhex(case['wire']);assert len(wire)==8463
        assert wire[:32]==b'SATOSHIFI_RESUME_WITNESS_V2'.ljust(32,b'\0');assert wire[32:35]==b'\0\1\0'
        raw=wire[35:115];context=wire[115:239];root=wire[239:271];proof=wire[271:]
        block_hash=context[:32];height=context[32:40];work=context[40:72];bits=context[72:76];epoch=context[76:80];times=context[80:124]
        assert hashlib.sha256(hashlib.sha256(raw).digest()).digest()==block_hash==unhex(case['head'])
        assert int.from_bytes(height,'big')==case['height'];assert work==unhex(case['work'])
        scoped=h(b'mainnet-anchor',b'bitcoin-mainnet',unhex(case['anchor']),case['anchor_height'].to_bytes(8,'big'))
        context_hash=h(b'citrea-LatestDaState-v1',block_hash,height,work,bits,epoch,times)
        node=h(b'known-header-v2',scoped,raw,height,work,context_hash,root)
        key=int.from_bytes(block_hash,'big')
        assert len(proof)==256*32
        for bit in range(256):
            sibling=proof[bit*32:(bit+1)*32];left,right=(sibling,node) if (key>>bit)&1 else (node,sibling)
            node=h(b'resume-index-node-v2',(bit+1).to_bytes(2,'big'),left,right)
        assert node==unhex(case['known_root'])
        assert h(b'canonical-root',scoped,block_hash,height,root)==unhex(case['canonical_root'])
        state=h(b'resume-state-v2',unhex(case['checkpoint_descriptor']),unhex(case['anchor']),case['anchor_height'].to_bytes(8,'big'),
            node,unhex(case['known_log']),case['known_count'].to_bytes(8,'big'),unhex(case['canonical_root']),block_hash,height,work,
            case['timestamp'].to_bytes(4,'big'),h(b'running'))
        assert state==unhex(case['state_commitment'])
def test_history_implementation_and_encoding_are_reused_byte_for_byte():
    spec=importlib.util.spec_from_file_location('resume_generate',HERE/'generate_reuse.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for path,text in module.generated():assert path.read_text()==text
    assert (HERE/'src/history.rs').read_bytes()==(HERE.parent/'fork-policy/src/history.rs').read_bytes()
def test_native_core_and_dependency_pins_are_unchanged():
    spec=importlib.util.spec_from_file_location('resume_qualify_test',HERE/'qualify.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert len(module.dependencies())==16
    _,original,corrected=module.prepare.checked_sources();native=module.native_source(original,corrected)
    assert (HERE/'native_tests.rs').read_text() in native
    assert str(HERE/'src/resume.rs') in native and str(HERE/'src/codec.rs') in native
    for name in ['transition-policy','checkpoint-policy','fork-policy','time-policy']:
        assert (HERE.parent/name/'native_tests.rs').read_text() in native

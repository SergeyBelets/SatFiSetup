"""Independent canonical-history encoding and source provenance checks."""
import hashlib
import importlib.util
import json
from pathlib import Path
from ops.zkspv.historical_sources import resolve

ROOT=Path(__file__).resolve().parents[2]
HERE=ROOT/'rust/zkspv/fork-policy'

def h(domain,*parts):
    prefix=b'SATOSHIFI_FORK_V1\0'+len(domain).to_bytes(4,'big')+domain
    return hashlib.sha256(prefix+b''.join(len(p).to_bytes(8,'big')+p for p in parts)).digest()

def verify(case):
    if case['halted'] or not case['anchor_height']<=case['height']<=case['head_height']: return False
    raw=bytes.fromhex(case['raw'][2:]); siblings=[bytes.fromhex(p[2:]) for p in case['siblings']]
    if len(raw)!=80 or len(siblings)!=64: return False
    scoped=h(b'mainnet-anchor',b'bitcoin-mainnet',bytes.fromhex(case['anchor'][2:]),case['anchor_height'].to_bytes(8,'big'))
    block_hash=hashlib.sha256(hashlib.sha256(raw).digest()).digest()
    node=h(b'canonical-header',scoped,case['height'].to_bytes(8,'big'),block_hash,raw[36:68],int.from_bytes(raw[68:72],'little').to_bytes(4,'big'))
    for bit,sibling in enumerate(siblings):
        pair=(sibling,node) if (case['height']>>bit)&1 else (node,sibling)
        node=h(b'history-node',bytes([bit+1]),*pair)
    return h(b'canonical-root',scoped,bytes.fromhex(case['head'][2:]),case['head_height'].to_bytes(8,'big'),node).hex()==case['canonical_root'][2:]

def test_shared_witnesses_match_independent_encoding_and_canonical_bounds():
    data=json.loads((ROOT/'web3/test/fixtures/fork-policy-v1.json').read_text())
    assert data['authentication'].startswith('NONE')
    assert len(data['proofs'])==9
    for case in data['proofs']: assert verify(case)==case['accepted'],case['name']

def test_every_graph_trace_agrees_with_independent_parent_walk_policy():
    data=json.loads((ROOT/'web3/test/fixtures/fork-policy-v1.json').read_text())
    assert sum(len(v) for v in data['traces'].values())==505
    for trace in data['traces'].values():
        parents=[0]; heights=[32256]; work=[0]; head=0; halted=False
        for step in trace:
            assert not halted
            parent,delta,expected_head,expected_height,expected_halt,expected_lca,expected_depth=step
            index=len(parents); parents.append(parent); heights.append(heights[parent]+1); work.append(work[parent]+delta)
            ancestors=set(); cursor=head
            while True:
                ancestors.add(cursor)
                if cursor==0: break
                cursor=parents[cursor]
            common=index
            while common not in ancestors: common=parents[common]
            depth=heights[head]-heights[common]
            if work[index]>work[head]:
                if depth>100: halted=True
                else: head=index
            assert (head,heights[head],int(halted),common,depth)==(expected_head,expected_height,expected_halt,expected_lca,expected_depth)

def test_pinned_selection_reference_and_fixture_bytes_are_unchanged():
    manifest=json.loads((HERE/'source-map.json').read_text())
    for name,expected in manifest['source_sha256'].items():
        assert hashlib.sha256(resolve(name,expected).read_bytes()).hexdigest()==expected,name

def test_new_policy_dependencies_match_existing_upstream_lock_exactly():
    spec=importlib.util.spec_from_file_location('fork_policy_qualify',HERE/'qualify.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert len(module.dependencies())==14
    _,original,corrected=module.prepare.checked_sources()
    native=module.native_source(original,corrected)
    assert (HERE/'native_tests.rs').read_text() in native
    assert str(HERE/'src/lib.rs') in native

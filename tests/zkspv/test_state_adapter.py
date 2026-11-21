"""Independent state/membership vectors and real IPaymentSPV ABI parity."""
import hashlib,importlib.util,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
HERE=ROOT/'rust/zkspv/state-adapter'

def framed(domain,*parts):
    return hashlib.sha256(b'SATOSHIFI_FORK_V1\0'+len(domain).to_bytes(4,'big')+domain+
        b''.join(len(p).to_bytes(8,'big')+p for p in parts)).digest()
def raw(value):return bytes.fromhex(value[2:])
def fixture():return json.loads((HERE/'fixtures/state-v2.json').read_text())
def words(journal):
    encoded=raw(journal);assert len(encoded)==1216
    return [encoded[i:i+32] for i in range(0,len(encoded),32)]
def state_hash(w,next_state):
    view,known=(15,35) if next_state else (11,32)
    halt=framed(b'running')
    if next_state and int.from_bytes(w[26],'big'):
        halt=framed(b'halt',w[28],w[29],w[30],w[31][-8:])
    return framed(b'resume-state-v2',w[4],w[5],w[6][-8:],w[known],w[known+1],w[known+2][-8:],
        w[10 if next_state else 9],w[view],w[view+1][-8:],w[view+2],w[view+3][-4:],halt)

def test_real_checkpoint_initial_state_and_sequence_are_bound_without_claiming_proofs():
    f=fixture();assert f['authentication'].startswith('NONE')
    baseline=json.loads((ROOT/'web3/test/fixtures/checkpoint-policy-v1.json').read_text())
    for cp,old in zip(f['checkpoints'],baseline['checkpoints'],strict=True):
        assert cp['height']==old['height'] and cp['raw']==old['raw'] and cp['work']==old['work']
        assert cp['prior']==old['prior']
        block_hash=hashlib.sha256(hashlib.sha256(raw(cp['raw'])).digest()).digest()
        time=raw(cp['raw'])[68:72][::-1]
        commitment=framed(b'resume-state-v2',raw(cp['descriptor']),block_hash,cp['height'].to_bytes(8,'big'),
            raw(cp['knownRoot']),raw(cp['knownLog']),(1).to_bytes(8,'big'),raw(cp['canonicalRoot']),block_hash,
            cp['height'].to_bytes(8,'big'),raw(cp['work']),time,framed(b'running'))
        assert commitment==raw(cp['commitment'])
    previous=raw(f['checkpoints'][0]['commitment'])
    for case in f['steps']:
        w=words(case['journal']);assert w[7]==previous==state_hash(w,False);assert w[8]==state_hash(w,True)
        assert int.from_bytes(w[37],'big')-int.from_bytes(w[34],'big')==int.from_bytes(w[23],'big')
        previous=w[8]
    last=words(f['steps'][-1]['journal'])
    assert int.from_bytes(last[26],'big')==1 and int.from_bytes(last[31],'big')==101
    assert int.from_bytes(last[27],'big')==2 and int.from_bytes(last[20],'big')==3

def test_membership_paths_authenticate_raw_header_and_current_canonical_root():
    f=fixture()
    for case in f['steps']+f['old_tip']+f['nonmonotonic']:
        w=words(case['journal']);scope=framed(b'mainnet-anchor',b'bitcoin-mainnet',w[5],w[6][-8:])
        for name in ['anchor','head']:
            p=case[name];header=raw(p['raw']);assert len(header)==80 and len(p['siblings'])==64
            block_hash=hashlib.sha256(hashlib.sha256(header).digest()).digest()
            node=framed(b'canonical-header',scope,p['height'].to_bytes(8,'big'),block_hash,header[36:68],header[68:72][::-1])
            for bit,sibling in enumerate(p['siblings']):
                left,right=(raw(sibling),node) if (p['height']>>bit)&1 else (node,raw(sibling))
                node=framed(b'history-node',bytes([bit+1]),left,right)
            assert framed(b'canonical-root',scope,w[15],w[16][-8:],node)==w[10]
    left,right=bytes([0x11])*32,bytes([0x22])*32
    assert hashlib.sha256(hashlib.sha256(left+right).digest()).digest()==raw(f['steps'][0]['head']['raw'])[36:68]

def test_compiled_adapter_matches_actual_consumer_interface():
    spec=importlib.util.spec_from_file_location('state_qualify',HERE/'qualify.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    abi=json.loads((HERE/'evidence/ZkSpvAdapter-abi.json').read_text())
    signatures=module.check_abi(abi)
    assert set(signatures)=={'checkTxInclusion','getBlockStatus','testHelpersEnabled'}
    source_map=json.loads((HERE/'source-map.json').read_text())
    for path,digest in source_map['prior_source_sha256'].items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest

def test_ordinary_local_receipts_are_distinct_from_positive_proof_acceptance():
    evidence=json.loads((HERE/'evidence/local-receipts.json').read_text())
    assert evidence['status']=='PASS_LOCAL_EVM' and not evidence['production_ready']
    assert evidence['actual_proof_acceptance']=='NOT_PERFORMED'
    assert evidence['public_transactions']==evidence['remote_requests']==0
    assert len(evidence['deployments'])==3
    for d in evidence['deployments']:
        assert d['creation_receipt']['status']==1 and 0<d['creation_receipt']['gas_used']<=16777216
        assert d['fake_seal_receipt']['status']==0 and d['initcode_bytes_with_arguments']<=49152
        for c in d['contracts'].values():
            assert c['runtime_bytes']<=24576 and c['ordinary_artifact_match_except_immutables']

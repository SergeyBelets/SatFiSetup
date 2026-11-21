"""Offline independent checks of real Core data and completed monetary evidence."""
import hashlib
import json
from pathlib import Path
from eth_abi import encode
from web3 import Web3
from ops.zkspv.state_trial import framed
from ops.zkspv.historical_sources import audit,resolve

ROOT=Path(__file__).resolve().parents[2]
HERE=ROOT/'rust/zkspv/deposit-integration'

def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def double(b):return hashlib.sha256(hashlib.sha256(b).digest()).digest()
def base():
    p=ROOT/read(HERE/'evidence/current.json')['directory']
    assert p.is_relative_to(HERE/'evidence')
    return p

def test_actual_core_pow_link_mtp_and_guest_binding():
    p=base();prep=read(p/'preparation.json');frame=(p/'state.input').read_bytes();b=frame[4:]
    assert int.from_bytes(frame[:4],'little')==len(b) and sha(p/'state.input')==prep['input_sha256']
    assert b[:32].rstrip(b'\0')==b'SATOSHIFI_REGTEST_DEPOSIT_V1'
    assert int.from_bytes(b[32:64],'big')==31337
    assert b[64:84]==bytes.fromhex(prep['addresses']['TestRegtestDepositState'][2:])
    assert b[84:1004]==(p/'checkpoint.bin').read_bytes() and b[1004]==6
    context=[b[i:i+80] for i in range(124,1004,80)]
    assert len(context)==11 and len(b)==1005+480
    limit=0x7fffff << (8*(0x20-3))
    previous=double(context[-1]);times=[int.from_bytes(h[68:72],'little') for h in context]
    for index,height in enumerate(range(2017,2023)):
        raw=b[1005+index*80:1085+index*80]
        assert raw==(p/f'block-{height}.bin').read_bytes()[:80]
        assert raw[4:36]==previous and raw[72:76]==bytes.fromhex('ffff7f20')
        assert int.from_bytes(double(raw),'little')<=limit
        timestamp=int.from_bytes(raw[68:72],'little');assert timestamp>sorted(times[-11:])[5]
        times.append(timestamp);previous=double(raw)
    journal=(p/'state.journal').read_bytes();w=[journal[i:i+32] for i in range(0,len(journal),32)]
    assert len(w)==16 and w[0].rstrip(b'\0')==b'SATOSHIFI_REGTEST_DEPOSIT_V1' and w[1].rstrip(b'\0')==b'bitcoin-regtest'
    assert w[3][12:]==b[64:84] and w[4]==hashlib.sha256(b[84:1004]).digest()
    assert w[7]==previous and int.from_bytes(w[8],'big')==2022
    assert int.from_bytes(w[9],'big')==int.from_bytes(b[92:124],'big')+12
    assert prep['program_image_id']!=read(ROOT/'rust/zkspv/guest-integration/report.json')['program_image_id']

def test_bitcoin_transaction_inclusion_and_exact_smp1_allocation():
    p=base();prep=read(p/'preparation.json');rows=read(p/'funding.json');header=(p/'block-2017.bin').read_bytes()[:80]
    from signer_agent import crypto
    for row in rows:
        decoded=crypto.call('bitcoin_funding',raw=row['raw_tx'],txid=row['txid'])
        assert not decoded['coinbase'] and decoded['stripped']==row['stripped_tx']
        txid=double(bytes.fromhex(row['stripped_tx']));assert txid[::-1].hex()==row['txid']
        assert double(header).hex()==row['block_hash']
        node=txid;index=row['index']
        for s in row['siblings']:
            other=bytes.fromhex(s);node=double(other+node if index&1 else node+other);index>>=1
        assert index==0 and node==header[36:68]
        # Wrong raw bytes cannot preserve this transaction identity.
        changed=bytearray.fromhex(row['stripped_tx']);changed[-1]^=1;assert double(changed)!=txid
        commitment=Web3.keccak(encode(['bytes32','uint256','address','uint32','address[]','uint256[]'],
            [Web3.keccak(text='SATOHSI_DEPOSIT_ALLOCATION_V1'),31337,prep['addresses']['MiningPoolV1'],row['vout'],[row['recipient']],[row['mp_amount']]]))
        script='6a38534d5031'+row['recipient'][2:].lower()+commitment.hex().removeprefix('0x')
        assert any(v['script']==script and int(v['amount'])==0 for v in decoded['outputs'])
        output=decoded['outputs'][row['vout']]
        assert int(output['amount'])==row['satoshis'] and row['mp_amount']==row['satoshis']*10**10
        assert output['script']=='5120'+prep['btc_public']['group'][2:]

def test_history_membership_independently_reconstructs_proven_root():
    p=base();j=(p/'state.journal').read_bytes();w=[j[i:i+32] for i in range(0,len(j),32)]
    scope=framed(b'mainnet-anchor',b'bitcoin-mainnet',w[5],int.from_bytes(w[6],'big').to_bytes(8,'big'))
    for height in range(2017,2023):
        raw=(p/f'block-{height}.bin').read_bytes()[:80];path=(p/f'history-{height}.bin').read_bytes()
        assert len(path)==2048
        node=framed(b'canonical-header',scope,height.to_bytes(8,'big'),double(raw),raw[36:68],raw[68:72][::-1])
        for bit in range(64):
            sibling=path[bit*32:(bit+1)*32]
            left,right=(sibling,node) if (height>>bit)&1 else (node,sibling)
            node=framed(b'history-node',bytes([bit+1]),left,right)
        assert framed(b'canonical-root',scope,w[7],int.from_bytes(w[8],'big').to_bytes(8,'big'),node)==w[11]

def test_genuine_proof_stages_and_source_bindings():
    p=base();prep=read(p/'preparation.json');previous=None
    # The mint predates the configurable mainnet bootstrap. Verify its original
    # sources, never rebind a historical receipt to today's consumer clients.
    scope=audit(prep['source_sha256'])
    assert not scope['current_source_qualification'] and not scope['production_ready']
    for name,digest in prep['source_sha256'].items():
        if name in scope['missing_tools']:
            assert name=='frost-crypto/target/debug/satohsi-frost-crypto'
            assert not scope['historical_reproduction_complete']
        else:assert sha(resolve(name,digest))==digest,name
    for stage in ['composite','succinct','groth16']:
        folder=p/stage;r=read(folder/'report.json');v=read(folder/'verified/receipt.json')
        assert r['status']=='VERIFIED' and r['source_unchanged'] and not r['dev_mode']
        assert r['supplemental_source_sha256']
        for name,digest in r['supplemental_source_sha256'].items():assert sha(ROOT/name)==digest,name
        assert r['preparation_sha256']==sha(p/'preparation.json') and r['prior_report_sha256']==previous
        assert v['status']=='VERIFIED' and v['receipt_kind']==stage and v['dev_mode_disabled']
        assert v['image_id']==prep['program_image_id'] and v['journal_sha256']==sha(p/'state.journal')
        assert v['receipt_sha256']==sha(folder/'receipt.bin') and v['wrong_image_rejected'] and v['modified_journal_rejected']
        previous=sha(folder/'report.json')
    assert r['container']['network_mode']=='none'

def test_real_mint_backing_replay_and_stale_rejection():
    p=base();a=read(p/'acceptance.json');prep=read(p/'preparation.json')
    assert a['status']=='LOCAL_REGTEST_ZK_DEPOSIT_PASS' and a['network']=='regtest'
    assert not a['production_ready'] and not a['mainnet_qualified'] and a['public_transactions']==0
    assert a['confirmations']==6 and a['after']['balance']==a['after']['supply']==a['after']['backing']*10**10
    assert a['after']['consumed'] and a['after']['nonce']==1 and a['second_deposit_unconsumed']
    assert a['consumer']==prep['addresses']['MiningPoolV1'] and a['ordinary_graph']==prep['graph']
    positive={'state_accepted','header_materialized','mint_accepted'}
    for name,receipt in a['checks'].items():
        assert receipt['status']==(1 if name in positive else 0),name
        assert receipt['gas_used']>0 and len(bytes.fromhex(receipt['transaction_hash'].removeprefix('0x')))==32
    assert prep['checks']['mint_before_proof_rejected']['status']==0
    assert a['cleanup']==dict(anvil='stopped',bitcoin='stopped')
    assert a['receipt_sha256']==sha(p/'groth16/receipt.bin') and a['journal_sha256']==sha(p/'state.journal')

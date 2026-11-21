"""Real state deployment, domain-bound input and proof-authenticated acceptance.

Only disposable loopback Anvil and public historical Bitcoin headers are used.
prepare leaves its node running for prove/accept; accept terminates that node.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

from web3 import Web3
from .wrap_local import sha, ROOT, GUEST, HERE as GROTH

HERE = ROOT / "rust/zkspv/state-proof-integration"
ARTIFACTS = Path("/private/tmp/satoshifi-groth16-evm-artifacts")
ANVIL = Path("/private/tmp/satohsi-tools/anvil")


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def framed(domain, *parts):
    return hashlib.sha256(b"SATOSHIFI_FORK_V1\0" + len(domain).to_bytes(4, "big") + domain +
        b"".join(len(p).to_bytes(8, "big") + p for p in parts)).digest()


def journal_state(journal, after):
    w = [journal[i:i+32] for i in range(0, 1216, 32)]
    v, k = (15, 35) if after else (11, 32)
    num = lambda i: int.from_bytes(w[i], "big")
    return (w[5], num(6), w[v], num(v+1), num(v+2), num(v+3), w[10 if after else 9],
            w[8 if after else 7], bool(num(26)) if after else False, w[k], w[k+1], num(k+2))


def rebind(frame, journal, address):
    if len(frame) != 31809 or int.from_bytes(frame[:4], "little") != len(frame)-4:
        raise ValueError("Unexpected qualified frame")
    if frame[4:36] != b"SATOSHIFI_TRANSITION_INPUT_V2".ljust(32,b"\0") or int.from_bytes(frame[36:68],"big") != 31337:
        raise ValueError("Unexpected input domain/chain")
    if frame[68:88] != bytes([0x11])*20 or len(journal) != 1216 or journal[96:128] != bytes(12)+bytes([0x11])*20:
        raise ValueError("Unexpected original address")
    if len(address) != 20 or address == bytes(20):
        raise ValueError("Actual nonzero deployed address required")
    return frame[:68]+address+frame[88:], journal[:96]+bytes(12)+address+journal[128:]


def membership(journal):
    """Independently reconstruct the two-leaf real mainnet canonical tree."""
    headers = (ROOT / "web3/test/fixtures/mainnet/headers-32246-36288.bin").read_bytes()
    raw = {h: headers[(h-32246)*80:(h-32245)*80] for h in (32256,32257)}
    state = journal_state(journal, True)
    scope = framed(b"mainnet-anchor", b"bitcoin-mainnet", state[0], (32256).to_bytes(8,"big"))
    nodes = {h: framed(b"canonical-header",scope,h.to_bytes(8,"big"),
        hashlib.sha256(hashlib.sha256(r).digest()).digest(),r[36:68],r[68:72][::-1]) for h,r in raw.items()}
    empty = framed(b"history-empty"); path=[]; index=32257
    for bit in range(64):
        path.append(nodes.get(index ^ 1, empty))
        parents={i>>1 for i in nodes}
        nodes={i:framed(b"history-node",bytes([bit+1]),nodes.get(i*2,empty),nodes.get(i*2+1,empty)) for i in parents}
        empty=framed(b"history-node",bytes([bit+1]),empty,empty);index>>=1
    if framed(b"canonical-root",scope,state[2],state[3].to_bytes(8,"big"),nodes[0]) != state[6]:
        raise ValueError("Independent canonical tree differs from guest journal")
    return raw[32257], path


def artifact(name):
    p=ARTIFACTS/(name+".sol")/(name+".json");a=read(p)
    pinned=read(ROOT/"rust/zkspv/state-adapter/report.json")["artifacts"][name]
    for field,key in (("bytecode","creation_sha256"),("deployedBytecode","runtime_template_sha256")):
        if hashlib.sha256(bytes.fromhex(a[field]["object"].removeprefix("0x"))).hexdigest()!=pinned[key]:
            raise ValueError("Qualified bytecode changed: "+name)
    return a


def runtime(w3, name, address):
    a=artifact(name);code=bytes(w3.eth.get_code(address));template=bytes.fromhex(a['deployedBytecode']['object'].removeprefix('0x'))
    if len(code)!=len(template) or len(code)>24576:raise ValueError("Runtime size mismatch")
    actual=bytearray(code);expected=bytearray(template)
    for refs in a['deployedBytecode'].get('immutableReferences',{}).values():
        for ref in refs:
            start,size=ref['start'],ref['length'];actual[start:start+size]=bytes(size);expected[start:start+size]=bytes(size)
    if actual!=expected:raise ValueError("Ordinary runtime mismatch")
    return dict(address=address,code_hash=Web3.keccak(code).hex(),runtime_bytes=len(code),ordinary_match=True)


def transact(w3, call, expected=1):
    tx=call.transact({'from':w3.eth.accounts[0],'gas':16777216})
    r=w3.eth.wait_for_transaction_receipt(tx,timeout=30)
    if r.status!=expected:raise ValueError("Unexpected transaction status")
    return dict(status=r.status,gas_used=r.gasUsed,transaction_hash=tx.hex(),block_number=r.blockNumber)


def snapshot():
    prior=read(GROTH/'report.json');hashes=dict(prior['source_sha256'])
    hashes[str((GROTH/'report.json').relative_to(ROOT))]=sha(GROTH/'report.json')
    for p in (Path(__file__).resolve(),ROOT/'ops/zkspv/state_trial_prove.py'):
        hashes[str(p.relative_to(ROOT))]=sha(p)
    for name,digest in hashes.items():
        if sha(ROOT/name)!=digest:raise ValueError('Source changed: '+name)
    return hashes


def prepare(out):
    hashes=snapshot();out.mkdir(parents=True,exist_ok=False)
    original=(GUEST/'evidence/mainnet-32257.journal').read_bytes()
    tip_time=journal_state(original,True)[5]
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    with (out/'anvil.log').open('w') as log:
        process=subprocess.Popen([ANVIL,'--host','127.0.0.1','--port',str(port),'--chain-id','31337',
            '--timestamp',str(tip_time-1000),'--hardfork','osaka','--gas-limit','16777216','--silent'],
            stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    try:
        w3=Web3(Web3.HTTPProvider(f'http://127.0.0.1:{port}',request_kwargs={'timeout':20}))
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError('Anvil exited')
            if w3.is_connected():break
            time.sleep(.05)
        else:raise RuntimeError('Anvil unavailable')
        a=artifact('ZkBitcoinStateV2');cp=read(ROOT/'rust/zkspv/state-adapter/fixtures/state-v2.json')['checkpoints'][0]
        image=bytes.fromhex(read(GUEST/'report.json')['program_image_id'])
        cfg=(bytes(31)+b'\x07',image,b'PROOF_ONLY_V1'.ljust(32,b'\0'),3600,7200,7200)
        factory=w3.eth.contract(abi=a['abi'],bytecode=a['bytecode']['object']);deployments=[]
        for _ in range(2):
            call=factory.constructor(cfg,bytes.fromhex(cp['raw'][2:]),cp['height'],int(cp['work'],16),[bytes.fromhex(v[2:]) for v in cp['prior']])
            result=transact(w3,call);receipt=w3.eth.get_transaction_receipt(result['transaction_hash'])
            state=w3.eth.contract(address=receipt.contractAddress,abi=a['abi'])
            assert tuple(state.functions.getState().call())==journal_state(original,False)
            assert not state.functions.isFresh().call() and state.functions.stateVersion().call()==0
            graph={name:runtime(w3,name,address) for name,address in [('ZkBitcoinStateV2',state.address),('RiscZeroGroth16Verifier',state.functions.verifier().call()),('ZkSpvAdapter',state.functions.adapter().call())]}
            deployments.append(dict(address=state.address,receipt=result,graph=graph))
        state=w3.eth.contract(address=deployments[0]['address'],abi=a['abi'])
        seal=(GROTH/'evidence/groth16-local/verified/seal.bin').read_bytes()
        rejected=transact(w3,state.functions.submitTransition(original,seal),0)
        assert tuple(state.functions.getState().call())==journal_state(original,False) and state.functions.stateVersion().call()==0
        frame,journal=rebind((GUEST/'evidence/mainnet-32257.input').read_bytes(),original,bytes.fromhex(state.address[2:]))
        membership(journal)
        (out/'state.input').write_bytes(frame);(out/'state.journal').write_bytes(journal)
        report=dict(schema='satoshifi.state-proof-preparation.v1',status='PREPARED',production_ready=False,
            source_sha256=hashes,node_pid=process.pid,rpc_url=w3.provider.endpoint_uri,chain_id=31337,
            anvil_sha256=sha(ANVIL),deployments=deployments,prior_genuine_wrong_address_rejected=rejected,
            input_sha256=sha(out/'state.input'),journal_sha256=sha(out/'state.journal'),program_image_id=image.hex(),
            journal_derivation='Qualified native journal with only deployment address rebound; authenticated by the new proof before acceptance',
            input_derivation='Qualified framed input with only its 20-byte deployment address changed; header/state/witness bytes identical',
            clock='Disposable historical Anvil clock for Fresh checks; no live-network freshness claim',public_transactions=0)
        write(out/'preparation.json',report);print(json.dumps(dict(status='PREPARED',state=state.address,node_pid=process.pid)))
    except BaseException:
        process.terminate();process.wait(timeout=5);raise


def accept(out):
    prep=read(out/'preparation.json');journal=(out/'state.journal').read_bytes()
    for p,digest in prep['source_sha256'].items():assert sha(ROOT/p)==digest,p
    proof=read(out/'groth16/verified/receipt.json')
    assert proof['receipt_kind']=='groth16' and proof['status']=='VERIFIED' and proof['journal_sha256']==sha(out/'state.journal')
    w3=Web3(Web3.HTTPProvider(prep['rpc_url'],request_kwargs={'timeout':20}));assert w3.eth.chain_id==31337
    abi=artifact('ZkBitcoinStateV2')['abi'];state=w3.eth.contract(address=prep['deployments'][0]['address'],abi=abi)
    other=w3.eth.contract(address=prep['deployments'][1]['address'],abi=abi)
    for d in prep['deployments']:
        for name,record in d['graph'].items():assert runtime(w3,name,record['address'])==record
    before=journal_state(journal,False);after=journal_state(journal,True)
    assert tuple(state.functions.getState().call())==before and tuple(other.functions.getState().call())==before
    seal=(out/'groth16/verified/seal.bin').read_bytes()
    receipts={}
    try:
        response=w3.provider.make_request('evm_setNextBlockTimestamp',[after[5]+60])
        if 'error' in response:raise RuntimeError(response['error'])
        receipts['wrong_state_rejected']=transact(w3,other.functions.submitTransition(journal,seal),0)
        assert tuple(other.functions.getState().call())==before and other.functions.stateVersion().call()==0
        receipts['transition_accepted']=transact(w3,state.functions.submitTransition(journal,seal))
        assert tuple(state.functions.getState().call())==after and state.functions.stateVersion().call()==1
        assert state.functions.hasUsefulUpdate().call() and state.functions.isFresh().call()
        accepted_at=state.functions.lastUsefulAcceptedAt().call()
        receipts['replay_rejected']=transact(w3,state.functions.submitTransition(journal,seal),0)
        altered=bytearray(journal);altered[8*32]^=1
        receipts['modified_journal_rejected']=transact(w3,state.functions.submitTransition(bytes(altered),seal),0)
        assert tuple(state.functions.getState().call())==after and state.functions.stateVersion().call()==1
        assert state.functions.lastUsefulAcceptedAt().call()==accepted_at
        adapter=w3.eth.contract(address=state.functions.adapter().call(),abi=artifact('ZkSpvAdapter')['abi'])
        assert not adapter.functions.testHelpersEnabled().call()
        assert adapter.functions.getBlockStatus(after[2]).call()==[False,0]
        raw,path=membership(journal)
        receipts['header_materialized']=transact(w3,adapter.functions.materializeHeader(32257,raw,path))
        assert adapter.functions.getBlockStatus(after[2]).call()==[True,1]
        cached=adapter.functions.materializedHeaders(after[2]).call();assert cached[0]==after[6] and cached[1]==raw[36:68] and cached[2]==32257
        w3.provider.make_request('evm_setNextBlockTimestamp',[accepted_at+3601]);w3.provider.make_request('evm_mine',[])
        assert not state.functions.isFresh().call() and adapter.functions.getBlockStatus(after[2]).call()==[False,0]
        assert not adapter.functions.checkTxInclusion(after[2],raw[36:68],[],0).call()
        report=dict(schema='satoshifi.state-proof-acceptance.v1',status='LOCAL_STATE_PROOF_PASS',production_ready=False,
            state=state.address,other_state=other.address,verifier=state.functions.verifier().call(),adapter=adapter.address,
            program_image_id=prep['program_image_id'],journal_sha256=sha(out/'state.journal'),receipt_sha256=proof['receipt_sha256'],
            checks=receipts,accepted_height=after[3],state_version=1,real_header_materialized=True,
            fresh_after_acceptance=True,stale_after_state_age=True,source_sha256=prep['source_sha256'],
            public_transactions=0,scope='One actual mainnet append on disposable historical-clock Anvil; no production/consumer or full scenario qualification')
        write(out/'acceptance.json',report);print(json.dumps(dict(status=report['status'],checks=receipts)))
    finally:
        # Verify the exact task-owned node identity before terminating it.
        result=subprocess.run(['ps','-p',str(prep['node_pid']),'-o','comm='],capture_output=True,text=True)
        if result.returncode==0 and result.stdout.strip()==str(ANVIL):os.kill(prep['node_pid'],signal.SIGTERM)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['prepare','accept']);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();(prepare if a.phase=='prepare' else accept)(a.output.resolve())

#!/usr/bin/env python3
"""Isolated local Anvil receipts, real pinned verifier negative path; no real proof."""
import argparse,hashlib,json,socket,subprocess,time
from pathlib import Path
from web3 import Web3

HERE=Path(__file__).resolve().parent
def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['anvil','artifacts','output']:p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();fixtures=json.loads((HERE/'fixtures/state-v2.json').read_text())
    def artifact(name):return json.loads(next(a.artifacts.glob('*/'+name+'.json')).read_text())
    state_artifact=artifact('ZkBitcoinStateV2')
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    command=[str(a.anvil),'--host','127.0.0.1','--port',str(port),'--chain-id','31337','--hardfork','osaka','--gas-limit','16777216','--silent']
    process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        w3=Web3(Web3.HTTPProvider(f'http://127.0.0.1:{port}',request_kwargs={'timeout':20}))
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError('Local Anvil exited: '+process.stderr.read().decode())
            if w3.is_connected():break
            time.sleep(.05)
        else:raise RuntimeError('Local Anvil unavailable')
        assert w3.eth.chain_id==31337
        sender=w3.eth.accounts[0] # unlocked, disposable local Anvil account; no user key
        factory=w3.eth.contract(abi=state_artifact['abi'],bytecode=state_artifact['bytecode']['object'])
        config=(bytes(31)+b'\x07',bytes(31)+b'\x7b',b'PROOF_ONLY_V1'.ljust(32,b'\0'),3600,7200,7200)
        deployments=[]
        for cp in fixtures['checkpoints']:
            constructor=factory.constructor(config,bytes.fromhex(cp['raw'][2:]),cp['height'],int(cp['work'],16),[bytes.fromhex(v[2:]) for v in cp['prior']])
            initcode=constructor.data_in_transaction;assert len(bytes.fromhex(initcode[2:]))<=49152
            tx=w3.eth.send_transaction({'from':sender,'data':initcode,'gas':16777216})
            receipt=w3.eth.wait_for_transaction_receipt(tx,timeout=30);assert receipt.status==1,'State deployment failed'
            state=w3.eth.contract(address=receipt.contractAddress,abi=state_artifact['abi'])
            initial=state.functions.getState().call();assert initial[7].hex()==cp['commitment'][2:]
            assert state.functions.stateVersion().call()==0 and not state.functions.hasUsefulUpdate().call() and not state.functions.isFresh().call()
            verifier=state.functions.verifier().call();adapter=state.functions.adapter().call()
            code_records={}
            for name,address in [('ZkBitcoinStateV2',receipt.contractAddress),('ZkSpvAdapter',adapter),('RiscZeroGroth16Verifier',verifier)]:
                art=artifact(name);code=bytes(w3.eth.get_code(address));template=bytes.fromhex(art['deployedBytecode']['object'][2:])
                assert len(code)==len(template)<=24576
                actual=bytearray(code);expected=bytearray(template)
                for refs in art['deployedBytecode'].get('immutableReferences',{}).values():
                    for ref in refs:
                        start,end=ref['start'],ref['start']+ref['length'];actual[start:end]=bytes(ref['length']);expected[start:end]=bytes(ref['length'])
                assert actual==expected,'Runtime differs from ordinary artifact outside declared immutables'
                code_records[name]={'address':address,'runtime_bytes':len(code),'runtime_keccak256':Web3.keccak(code).hex(),
                    'runtime_sha256':hashlib.sha256(code).hexdigest(),'ordinary_artifact_match_except_immutables':True,
                    'creation_bytecode_sha256':hashlib.sha256(bytes.fromhex(art['bytecode']['object'][2:])).hexdigest(),
                    'runtime_template_sha256':hashlib.sha256(template).hexdigest()}
            assert bytes(state.functions.verifierCodeHash().call())==Web3.keccak(w3.eth.get_code(verifier))
            # Validly sized journal and correctly routed selector, but no proof.
            journal=bytearray.fromhex(fixtures['steps'][0]['journal'][2:]);journal[3*32:4*32]=bytes(12)+bytes.fromhex(receipt.contractAddress[2:])
            v=w3.eth.contract(address=verifier,abi=artifact('RiscZeroGroth16Verifier')['abi'])
            seal=bytes(v.functions.SELECTOR().call())+bytes(256)
            data=state.functions.submitTransition(bytes(journal),seal)._encode_transaction_data()
            bad=w3.eth.send_transaction({'from':sender,'to':receipt.contractAddress,'data':data,'gas':1000000})
            rejected=w3.eth.wait_for_transaction_receipt(bad,timeout=30);assert rejected.status==0,'Fake seal unexpectedly accepted'
            assert state.functions.getState().call()==initial and state.functions.stateVersion().call()==0
            deployments.append({'checkpoint_height':cp['height'],'state':receipt.contractAddress,'initcode_bytes_with_arguments':len(bytes.fromhex(initcode[2:])),
                'creation_receipt':dict(status=receipt.status,gas_used=receipt.gasUsed,transaction_hash=tx.hex()),
                'fake_seal_receipt':dict(status=rejected.status,gas_used=rejected.gasUsed,transaction_hash=bad.hex()),'contracts':code_records})
        result=dict(schema='satoshifi.state-adapter-local-receipts.v2',status='PASS_LOCAL_EVM',production_ready=False,
            evm='disposable loopback Anvil, Osaka, chain 31337, 16777216 gas cap',fixture_image_id='0x'+'00'*31+'7b',
            script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),fixture_sha256=hashlib.sha256((HERE/'fixtures/state-v2.json').read_bytes()).hexdigest(),
            deployments=deployments,actual_proof_acceptance='NOT_PERFORMED',remote_requests=0,public_transactions=0,
            scope='Ordinary bytecode deployment and real-verifier rejection only; synthetic positive tests are separately identified')
        a.output.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({'status':result['status'],'creation_gas':[d['creation_receipt']['gas_used'] for d in deployments],'fake_proofs_rejected':len(deployments)}))
    finally:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
if __name__=='__main__':main()

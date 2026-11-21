"""TEST_ONLY synthetic-state receiver; never deploy to a public chain."""
import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from web3 import Web3
from .scenario_prepare import HERE,prepare
from .state_trial import ROOT,ANVIL,GUEST,read,write,journal_state,transact
from .evm_receipt import checked_bundle
from .wrap_local import sha

NAMES=['fork-work-reorg100','halt101-prefix']
ARTIFACTS=HERE/'evidence/model-contract-artifacts'

def artifact(name):
    path=ARTIFACTS/(name+'.sol')/(name+'.json');build=read(HERE/'model-build-report.json')
    assert sha(path)==build['evm_artifacts'][name]['sha256']
    return read(path)

def runtime(w3,name,address):
    a=artifact(name);actual=bytearray(w3.eth.get_code(address));expected=bytearray.fromhex(a['deployedBytecode']['object'].removeprefix('0x'))
    assert len(actual)==len(expected)<=24576
    digest=hashlib.sha256(actual).hexdigest()
    for refs in a['deployedBytecode'].get('immutableReferences',{}).values():
        for ref in refs:
            at,n=ref['start'],ref['length'];actual[at:at+n]=bytes(n);expected[at:at+n]=bytes(n)
    assert actual==expected
    return dict(address=address,runtime_sha256=digest,ordinary_match=True)

def prepare_all():
    build=read(HERE/'model-build-report.json');a=artifact('TestPolicyReceiver')
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    node=HERE/'evidence/model-deployments.json';assert not node.exists()
    with (HERE/'evidence/model-anvil.log').open('w') as log:
        process=subprocess.Popen([str(ANVIL),'--host','127.0.0.1','--port',str(port),'--chain-id','31337','--hardfork','osaka',
            '--gas-limit','16777216','--timestamp','1300000000','--silent'],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    try:
        w3=Web3(Web3.HTTPProvider(f'http://127.0.0.1:{port}',request_kwargs={'timeout':20}))
        for _ in range(100):
            if process.poll() is not None:raise RuntimeError('Anvil exited')
            if w3.is_connected():break
            time.sleep(.05)
        else:raise RuntimeError('Anvil unavailable')
        assert w3.eth.chain_id==31337;deployments={}
        factory=w3.eth.contract(abi=a['abi'],bytecode=a['bytecode']['object'])
        for name in NAMES:
            prepare(name);base=HERE/'evidence'/name;p=read(base/'preparation.json')
            journal=(base/'state.journal').read_bytes();initial=journal_state(journal,False)
            receipt=transact(w3,factory.constructor(bytes.fromhex(build['program_image_id']),journal[128:160],initial))
            address=w3.eth.get_transaction_receipt(receipt['transaction_hash']).contractAddress
            contract=w3.eth.contract(address=address,abi=a['abi']);assert tuple(contract.functions.getState().call())==initial
            graph={key:runtime(w3,key,addr) for key,addr in [('TestPolicyReceiver',address),('RiscZeroGroth16Verifier',contract.functions.verifier().call())]}
            frame=(base/'state.input').read_bytes();assert frame[68:88]==bytes([0x11])*20 and journal[96:128]==bytes(12)+bytes([0x11])*20
            address_bytes=bytes.fromhex(address[2:]);frame=frame[:68]+address_bytes+frame[88:];journal=journal[:96]+bytes(12)+address_bytes+journal[128:]
            (base/'state.input').write_bytes(frame);(base/'state.journal').write_bytes(journal)
            p.update(input_sha256=sha(base/'state.input'),journal_sha256=sha(base/'state.journal'),model_state=address)
            p['source_sha256'][str(Path(__file__).resolve().relative_to(ROOT))]=sha(Path(__file__))
            write(base/'preparation.json',p);deployments[name]=dict(address=address,receipt=receipt,graph=graph)
        result=dict(status='TEST_ONLY_DEPLOYED',rpc_url=w3.provider.endpoint_uri,node_pid=process.pid,anvil_sha256=sha(ANVIL),deployments=deployments,
            scope='Synthetic initial states; historical disposable clock; no mainnet checkpoint authentication or production activation')
        write(node,result)
        for name in NAMES:
            p=read(HERE/'evidence'/name/'preparation.json');p['model_deployments_sha256']=sha(node);write(HERE/'evidence'/name/'preparation.json',p)
        print(json.dumps(dict(status=result['status'],node_pid=process.pid)))
    except BaseException:
        process.terminate();process.wait(timeout=10);raise

def accept(name):
    base=HERE/'evidence'/name;p=read(base/'preparation.json');node=read(HERE/'evidence/model-deployments.json')
    assert sha(HERE/'evidence/model-deployments.json')==p['model_deployments_sha256']
    for path,h in p['source_sha256'].items():assert sha(ROOT/path)==h,path
    metadata=checked_bundle(base/'groth16/verified',dict(image_id=p['program_image_id'],program_sha256=p['program_sha256'],journal_sha256=p['journal_sha256']))
    w3=Web3(Web3.HTTPProvider(node['rpc_url'],request_kwargs={'timeout':20}));assert w3.eth.chain_id==31337
    deployment=node['deployments'][name]
    for key,value in deployment['graph'].items():assert runtime(w3,key,value['address'])==value
    contract=w3.eth.contract(address=deployment['address'],abi=artifact('TestPolicyReceiver')['abi'])
    journal=(base/'state.journal').read_bytes();seal=(base/'groth16/verified/seal.bin').read_bytes()
    assert sha(base/'state.journal')==metadata['journal_sha256']
    before=journal_state(journal,False);after=journal_state(journal,True)
    assert tuple(contract.functions.getState().call())==before
    now=w3.eth.get_block('latest').timestamp;target=max(now+1,1300100060)
    response=w3.provider.make_request('evm_setNextBlockTimestamp',[target]);assert 'error' not in response
    checks={}
    checks['production_decoder_rejects_model']=transact(w3,contract.functions.productionDecoderRejects(journal),0)
    verifier=w3.eth.contract(address=contract.functions.verifier().call(),abi=artifact('RiscZeroGroth16Verifier')['abi'])
    checks['mainnet_image_rejects_model']=transact(w3,verifier.functions.verify(seal,bytes.fromhex(read(GUEST/'report.json')['program_image_id']),bytes.fromhex(p['journal_sha256'])),0)
    checks['accepted']=transact(w3,contract.functions.submit(journal,seal))
    assert tuple(contract.functions.getState().call())==after and contract.functions.stateVersion().call()==1
    checks['replay_rejected']=transact(w3,contract.functions.submit(journal,seal),0)
    altered=bytearray(journal);altered[8*32]^=1
    checks['modified_journal_rejected']=transact(w3,contract.functions.submit(bytes(altered),seal),0)
    assert tuple(contract.functions.getState().call())==after and contract.functions.stateVersion().call()==1
    if name=='halt101-prefix':
        assert after[8] and after[3]==32357 and contract.functions.haltDepth().call()==101 and contract.functions.haltPosition().call()==2
        assert not contract.functions.isFresh().call()
    else:assert not after[8] and after[3]==32257 and contract.functions.isFresh().call()
    write(base/'model-acceptance.json',dict(status='TEST_ONLY_STATE_PROOF_PASS',scenario=name,state=contract.address,checks=checks,
        image_id=metadata['image_id'],receipt_sha256=metadata['receipt_sha256'],journal_sha256=metadata['journal_sha256'],
        state_version=1,halted=after[8],production_ready=False,public_transactions=0,scope=p['starting_state_scope']))
    print(json.dumps(dict(scenario=name,status='TEST_ONLY_STATE_PROOF_PASS',checks=checks)))
    if all((HERE/'evidence'/item/'model-acceptance.json').exists() for item in NAMES):
        identity=subprocess.run(['ps','-p',str(node['node_pid']),'-o','comm='],capture_output=True,text=True)
        if identity.returncode==0 and identity.stdout.strip()==str(ANVIL):os.kill(node['node_pid'],signal.SIGTERM)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['prepare','accept']);p.add_argument('--scenario',choices=NAMES)
    a=p.parse_args();prepare_all() if a.phase=='prepare' else accept(a.scenario)

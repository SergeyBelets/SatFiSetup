"""Requires local Anvil and built artifacts. Each participant command is a separate process.
Only explicitly artificial EVM identities are created by this harness. DKG secrets stay in participant files.
"""
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from web3 import Web3
from eth_account import Account
from signer_agent.dkg_protocol import Manifest, PROTOCOL_ID

ROOT=Path(__file__).resolve().parents[2]
RPC=os.environ.get('SATOHSI_TEST_RPC','http://127.0.0.1:18545')


def artifact(name):
    paths=list((ROOT/'web3/contracts/out').glob(f'*/{name}.json'))
    if len(paths)!=1:raise ValueError('ARTIFACT_NOT_UNIQUE: '+name)
    return json.loads(paths[0].read_text())


def deploy(w3,name,*args):
    a=artifact(name)
    c=w3.eth.contract(abi=a['abi'],bytecode=a['bytecode']['object'])
    receipt=w3.eth.wait_for_transaction_receipt(c.constructor(*args).transact({'from':w3.eth.accounts[0]}))
    assert receipt.status==1
    return w3.eth.contract(address=receipt.contractAddress,abi=a['abi'])


def tx(w3,f,*,sender=None):
    receipt=w3.eth.wait_for_transaction_receipt(f.transact({'from':sender or w3.eth.accounts[0]}))
    assert receipt.status==1
    return receipt


def authorize_ceremony(w3, registry, session_id, creator):
    registrar = registry.functions.ceremonyRegistrar().call()
    if registrar != creator:
        tx(w3, registry.functions.authorizeCeremonyCreator(session_id, creator), sender=registrar)


def cli(*args):
    p=subprocess.run([sys.executable,'-m','signer_agent.cli','dkg',*map(str,args)],capture_output=True,text=True,timeout=180)
    if p.returncode: raise AssertionError(p.stdout+'\n'+p.stderr)
    return json.loads(p.stdout)


def ceremony(tmp_path,w3,registry,n,t,purpose):
    dirs=[]; addresses=[]; transport=[]
    for i in range(1,n+1):
        d=tmp_path/str(i);d.mkdir()
        account=Account.from_key(i.to_bytes(32,'big'))  # artificial EVM identities, never FROST shares
        addresses.append(account.address)
        w3.provider.make_request('anvil_setBalance',[account.address,hex(10**22)])
        (d/'identity.json').write_text(json.dumps({'private_key':('%064x'%i)}));os.chmod(d/'identity.json',0o600)
        (d/'password').write_text(os.urandom(32).hex());os.chmod(d/'password',0o600)
        transport.append(cli('transport-init','--state',d/'state.enc','--password-file',d/'password')['public'])
        dirs.append(d)
    m=Manifest(w3.eth.chain_id,registry.address,os.urandom(32),1,purpose,tuple(addresses),tuple(bytes.fromhex(x) for x in transport),t)
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps(m.to_dict()))
    base=['--manifest',manifest,'--rpc',RPC]
    authorize_ceremony(w3, registry, m.session_id, m.participants[0])
    cli('create',*base,'--identity-file',dirs[0]/'identity.json')
    def participant(i,action,*extra):
        d=dirs[i]
        return cli(action,*base,'--identity-file',d/'identity.json','--state',d/'state.enc','--password-file',d/'password','--mailbox',tmp_path/'mailbox.sqlite',*extra)
    for _ in range(8):
        statuses=[participant(i,'run-once')['status'] for i in range(n)]
        if all(x=='ACTIVE' for x in statuses): break
    assert statuses==['ACTIVE']*n
    for i in range(n): assert participant(i,'verify')['status']=='VERIFIED'
    cli('export',*base,'--state',dirs[0]/'state.enc','--password-file',dirs[0]/'password','--format','public','--output',tmp_path/'public.json')
    return m,participant,json.loads((tmp_path/'public.json').read_text())


@pytest.mark.parametrize('n,t',[(3,2),(5,3)])
def test_real_dkg_then_evm_authorization(tmp_path,n,t,record_property):
    w3=Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id==31337, 'local profile only'
    registry=deploy(w3,'DKGCoordinator')
    verifier=deploy(w3,'FROSTVerifier')
    coordinator=deploy(w3,'FROSTCoordinator',verifier.address)
    tx(w3,coordinator.functions.bindDKGRegistry(registry.address))
    m,participant,public=ceremony(tmp_path,w3,registry,n,t,'EVM_AUTH')
    group=bytes.fromhex(public['group'])[1:]
    auth=deploy(w3,'DKGAuthorization',coordinator.address,m.session_id,group,list(m.participants),t)
    record_property("public_deployment",json.dumps(dict(registry=registry.address,coordinator=coordinator.address,authorization=auth.address,manifest=m.to_dict(),certificate=public)))
    # Every t-subset uses original manifest indexes, including non-contiguous subsets.
    for subset in itertools.combinations(range(n),t):
        target=auth.address
        params=Web3.keccak(text=str(subset));selector=bytes.fromhex('12345678')
        hash_args=[target,'0x'+selector.hex(),'0x'+params.hex(),0]
        message=auth.functions.hashOperation(*hash_args).call()
        deadline=w3.eth.get_block('latest').timestamp+3600
        tx(w3,coordinator.functions.createSessionFromDKG(m.session_id,message,target,deadline))
        sid=coordinator.functions.nextSessionId().call()
        intent=tmp_path/'intent.json';intent.write_text(json.dumps(dict(chain_id='31337',target=target,operation_hash=message.hex(),artifact='DKGAuthorization',hash_method='hashOperation',hash_args=hash_args)))
        args=['--coordinator',coordinator.address,'--session',sid,'--package',intent]
        # Non-selected participants explicitly refuse so fallback selection is deterministic.
        for i in range(n):
            if i not in subset:
                f=coordinator.functions.rejectSignatureRequest(sid,'test subset')
                transaction=f.build_transaction({'from':m.participants[i],'nonce':w3.eth.get_transaction_count(m.participants[i]),'chainId':31337})
                signed=Account.sign_transaction(transaction,(i+1).to_bytes(32,'big'))
                assert w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed.raw_transaction)).status==1
        for i in subset: assert participant(i,'sign-evm',*args)['status']=='committed'
        for i in subset: assert participant(i,'sign-evm',*args)['status'] in ('signed','finalized')
        tx(w3,auth.functions.authorizeOperation(sid,message,target))
        assert auth.functions.isOperationApproved(message,target).call()

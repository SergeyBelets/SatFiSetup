"""Fresh zero-balance participants, explicit funding release and delayed native DKG."""
from concurrent.futures import ThreadPoolExecutor
import json,os,subprocess,sys
from pathlib import Path
import pytest
from eth_account import Account
from web3 import Web3
from signer_agent.dkg_protocol import Manifest
from test_dkg_processes import RPC,authorize_ceremony,deploy,tx


def account(tmp_path,name):
    person=Account.create();path=tmp_path/(name+'.json')
    path.write_text(json.dumps(dict(private_key=person.key.hex())));path.chmod(0o600)
    return person,path


def fund(w3,to,amount=10**18):
    r=w3.eth.wait_for_transaction_receipt(w3.eth.send_transaction({'from':w3.eth.accounts[0],'to':to,'value':amount}))
    assert r.status==1


def command(*args,expected=(0,)):
    p=subprocess.run([sys.executable,'-m','signer_agent.cli',*map(str,args)],capture_output=True,text=True,timeout=120)
    assert p.returncode in expected,p.stdout+p.stderr
    return json.loads(p.stdout)


def test_zero_balance_pause_waits_for_all_confirmed_funding_then_starts_dkg(tmp_path,record_property):
    w3=Web3(Web3.HTTPProvider(RPC));assert w3.eth.chain_id==31337
    people=[account(tmp_path,'identity'+str(i)) for i in range(3)]
    addresses=[a.address for a,_ in people];operator,identity=people[0]
    assert all(w3.eth.get_balance(a)==0 for a in addresses)
    # Actual native transfers fund gas; no anvil_setBalance is used for participants.
    fund(w3,operator.address)
    registry=deploy(w3,'DKGCoordinator');sid=os.urandom(32)
    authorize_ceremony(w3, registry, sid, operator.address)
    profile=dict(deployment_id='funding-checkpoint',chain_id='31337',mode='integration',bitcoin_network='regtest',
        evm_rpc=RPC,evm_confirmations='2',operator=operator.address,evm_ceremony=dict(participants=addresses),
        contracts=dict(dkg=dict(address=registry.address,artifact='DKGCoordinator')),
        steps=[dict(id='create',kind='call',contract='dkg',method='createCeremony',args=[
            '0x'+sid.hex(),addresses,'3',['0x'+(i+1).to_bytes(32,'big').hex() for i in range(3)],'1',
            '0x'+Web3.keccak(text='EVM_AUTH').hex().removeprefix('0x'),
            '0x'+__import__('signer_agent.dkg_protocol',fromlist=['PROTOCOL_ID']).PROTOCOL_ID.hex()])])
    state=tmp_path/'bootstrap.sqlite';path=tmp_path/'profile.json';path.write_text(json.dumps(profile))
    def apply(*extra,member=0):return command('bootstrap','resume','--profile',path,'--state',state,
        '--identity-file',people[member][1],*extra,expected=(0,2))
    initial=apply('--pause-for-funding','--funding-min-wei',str(10**16),'--funding-operator-min-wei',str(10**17))
    assert initial['status']=='PAUSED_FOR_FUNDING'
    # Let more than a whole DKG phase elapse while paused: no ceremony exists.
    w3.provider.make_request('evm_increaseTime',[7200]);w3.provider.make_request('evm_mine',[])
    assert registry.functions.ceremonies(sid).call()[0]==bytes(32)
    assert apply()['status']=='PAUSED_FOR_FUNDING'
    assert apply('--funding-complete')['status']=='PAUSED_FOR_FUNDING'
    fund(w3,addresses[1]);assert apply('--funding-complete')['status']=='PAUSED_FOR_FUNDING'
    fund(w3,addresses[2]);assert apply('--funding-complete')['status']=='PAUSED_FOR_FUNDING'
    # The final native funding transfer must reach the enrolled depth first.
    w3.provider.make_request('evm_mine',[])
    assert apply()['status']=='PAUSED_FOR_FUNDING' # funding alone never releases
    assert apply('--funding-complete',member=1)['code']=='BOOTSTRAP_FUNDING_OPERATOR_REQUIRED'
    result=apply('--funding-complete');assert result['status'] in ('CONFIRMING','VERIFIED')
    w3.provider.make_request('evm_mine',[])
    assert apply()['status']=='VERIFIED'
    info=registry.functions.getSessionInfo(sid).call()
    assert info[4]==1 and info[5]>w3.eth.get_block('latest').timestamp
    assert w3.eth.get_transaction_count(operator.address)==1
    assert apply()['status']=='VERIFIED' and w3.eth.get_transaction_count(operator.address)==1
    record_property('pause_seconds_before_dkg',7200)
    record_property('funding','Native transfers from local fixture faucet; all participant balances initially zero')


def test_delayed_gas_funding_does_not_advance_dkg_and_duplicate_workers_are_serialized(tmp_path,record_property):
    w3=Web3(Web3.HTTPProvider(RPC));registry=deploy(w3,'DKGCoordinator')
    people=[];transport=[]
    for i in range(3):
        home=tmp_path/str(i);home.mkdir()
        person,identity=account(home,'identity');password=home/'password';password.write_text(os.urandom(32).hex());password.chmod(0o600)
        state=home/'state.enc'
        transport.append(bytes.fromhex(command('dkg','transport-init','--state',state,'--password-file',password)['public']))
        people.append((person,identity,state,password))
    addresses=[p[0].address for p in people]
    assert all(w3.eth.get_balance(a)==0 for a in addresses)
    fund(w3,addresses[0]);fund(w3,addresses[1])
    m=Manifest(31337,registry.address,os.urandom(32),1,'EVM_AUTH',tuple(addresses),tuple(transport),3)
    path=tmp_path/'manifest.json';path.write_text(json.dumps(m.to_dict()))
    authorize_ceremony(w3, registry, m.session_id, addresses[0])
    command('dkg','create','--manifest',path,'--rpc',RPC,'--identity-file',people[0][1])
    def participant(i,action='run-once',expected=(0,)):
        _,identity,state,password=people[i]
        return command('dkg',action,'--manifest',path,'--rpc',RPC,'--identity-file',identity,
            '--state',state,'--password-file',password,'--mailbox',tmp_path/'mailbox.sqlite',expected=expected)
    with ThreadPoolExecutor(max_workers=3) as pool:
        early=list(pool.map(lambda i:participant(i,expected=(0,2)),[0,0,1]))
    assert all(r['status']!='ERROR' or r['code']=='BlockingIOError' for r in early),early
    assert any(r['status']!='ERROR' for r in early[:2]) and early[2]['status']!='ERROR'
    assert registry.functions.getSessionInfo(m.session_id).call()[4]==1
    assert w3.eth.get_transaction_count(addresses[0])==2 # one create and one round1 despite two workers
    failed=participant(2,expected=(2,));assert failed['status']=='ERROR'
    assert registry.functions.round1Hashes(m.session_id,addresses[2]).call()==bytes(32)
    assert registry.functions.getSessionInfo(m.session_id).call()[4]==1
    record_property('zero_gas_error',failed['code'])
    fund(w3,addresses[2])
    for _ in range(12):
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(participant,range(3)))
        if all(r['status']=='ACTIVE' for r in results):break
    assert all(r['status']=='ACTIVE' for r in results),results
    assert registry.functions.getSessionInfo(m.session_id).call()[4]==4
    assert all(participant(i,'verify')['status']=='VERIFIED' for i in range(3))
    record_property('native_dkg_with_late_funding','ACTIVE for all three participants after ordinary native funding')


def test_missing_converter_tokens_stop_before_signing_then_resume_after_usd_funding(tmp_path):
    from signer_agent.bootstrap import Bootstrap
    w3=Web3(Web3.HTTPProvider(RPC));token=deploy(w3,'DAOTestUSD')
    sender,identity=account(tmp_path,'operator');fund(w3,sender.address)
    converter=Account.create().address;amount=1234567
    profile=dict(deployment_id='late-converter-funding',chain_id='31337',mode='integration',bitcoin_network='regtest',
        evm_rpc=RPC,evm_confirmations='1',contracts=dict(usd=dict(address=token.address,artifact='DAOTestUSD')),
        steps=[dict(id='fund',kind='call',contract='usd',method='transfer',args=[converter,str(amount)],
            verify_token_transfer=dict(token='usd',recipient=converter,amount=str(amount)))])
    state=tmp_path/'bootstrap.sqlite';b=Bootstrap(profile,state)
    from web3.exceptions import ContractLogicError, Web3RPCError
    with pytest.raises((ContractLogicError,Web3RPCError)):b.apply(identity)
    entry=b.journal.get('late-converter-funding:fund')
    assert 'raw_transaction' not in entry['result'] and w3.eth.get_transaction_count(sender.address)==0
    tx(w3,token.functions.mint(sender.address,amount))
    result=Bootstrap(profile,state).apply(identity)
    assert result['status']=='VERIFIED' and token.functions.balanceOf(converter).call()==amount
    assert Bootstrap(profile,state).apply(identity)['status']=='VERIFIED'
    assert token.functions.balanceOf(converter).call()==amount and w3.eth.get_transaction_count(sender.address)==1

"""Actual single issuance, manual transfers and staking during a durable ULR pause."""
import json
import pytest
from eth_account import Account
from web3 import Web3
from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_distribution import STATUS
from signer_agent.ulr_treasury import SUPPLY
from test_dkg_processes import RPC,deploy
from test_startup_funding import account,fund


def send(w3,person,function):
    tx=function.build_transaction({'from':person.address,'nonce':w3.eth.get_transaction_count(person.address,'pending'),
        'chainId':31337,'gas':500000})
    receipt=w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(person.sign_transaction(tx).raw_transaction))
    assert receipt.status==1
    return receipt


def prepare(tmp_path,depth,metadata=None):
    w3=Web3(Web3.HTTPProvider(RPC));assert w3.eth.chain_id==31337
    people=[account(tmp_path,'identity'+str(i)) for i in range(3)]
    operator,owner,member=[x[0] for x in people]
    fund(w3,operator.address);fund(w3,owner.address);fund(w3,member.address)
    usd=deploy(w3,'DAOTestUSD')
    profile=dict(deployment_id='manual-ulr',chain_id='31337',mode='integration',bitcoin_network='regtest',
        evm_rpc=RPC,evm_confirmations=str(depth),operator=operator.address,
        ulr=dict(version='1',mode='create',bitcoin_backend='relay'),steps=[
            dict(id='dao_token',kind='deploy',artifact='CustodianDAOToken',args=['ULR','ULR',owner.address],
                checks=[dict(contract='dao_token',method='totalSupply',args=[],equals=str(SUPPLY))]),
            dict(id='staking',kind='deploy',artifact='ULRStaking',args=['$dao_token',usd.address,'200000000000000000000000','1']),
            dict(id='ulr_distribution',kind='ulr_distribution',token='dao_token',staking='staking',
                participants=[p[0].address for p in people],issue_recipient=owner.address),
            dict(id='activation_sentinel',kind='deploy',artifact='FROSTVerifier',args=[])])
    if metadata:
        profile['token_metadata']=metadata
        profile['steps'][0]['args'][:2]=[metadata['governance']['name'],metadata['governance']['symbol']]
    journal=tmp_path/'bootstrap.sqlite'
    def runner():return Bootstrap(profile,journal)
    def apply(**kw):return runner().apply(people[0][1],**kw)
    with pytest.raises(ValueError,match='ULR_DISTRIBUTION_NOT_PAUSED'):
        apply(ulr_distribution_complete=True,accept_ulr_distribution='0x'+'11'*32)
    assert w3.eth.get_transaction_count(operator.address)==0
    for _ in range(6):
        result=apply()
        if result['status']==STATUS:break
        assert result['status']=='CONFIRMING';w3.provider.make_request('evm_mine',[])
    assert result['status']==STATUS
    return w3,people,runner,apply,result


@pytest.mark.parametrize('depth',[1,2])
def test_manual_ulr_distribution_after_mint_survives_restart_and_confirmation_delay(tmp_path,depth,record_property):
    w3,people,runner,apply,first=prepare(tmp_path,depth)
    operator,owner,member=[x[0] for x in people]
    b=runner();b.apply(people[0][1]);token,staking=b.contract('dao_token'),b.contract('staking')
    assert token.functions.totalSupply().call()==SUPPLY
    assert token.functions.balanceOf(owner.address).call()==SUPPLY
    assert token.functions.balanceOf(operator.address).call()==0
    assert w3.eth.get_transaction_count(operator.address)==2
    assert apply()['status']==runner().apply(verify=True)['status']==STATUS
    assert 'activation_sentinel' not in apply()['manifest']['contracts']
    old=first['distribution']['fingerprint']
    amount=200000*10**18
    send(w3,owner,token.functions.transfer(operator.address,amount))
    send(w3,owner,token.functions.transfer(member.address,amount))
    with pytest.raises(ValueError,match='ULR_DISTRIBUTION_CHANGED'):
        apply(ulr_distribution_complete=True,accept_ulr_distribution=old)
    pending=apply()
    if depth==2:
        assert not pending['distribution']['transfers_confirmed']
        assert apply(ulr_distribution_complete=True,accept_ulr_distribution=pending['distribution']['fingerprint'])['status']==STATUS
        assert w3.eth.get_transaction_count(operator.address)==2
        w3.provider.make_request('evm_mine',[])
    # Own stake is included, so moving ULR into staking does not reduce the displayed total.
    send(w3,member,token.functions.approve(staking.address,amount//2))
    send(w3,member,staking.functions.stake(amount//2))
    if depth==2:w3.provider.make_request('evm_mine',[])
    current=apply();view=current['distribution'];assert view['transfers_confirmed']
    rows={r['address']:r for r in view['participants']}
    assert rows[member.address]['wallet_ulr_wei']==rows[member.address]['own_stake_ulr_wei']==str(amount//2)
    assert rows[member.address]['total_ulr_wei']==str(amount) and rows[member.address]['at_least_200k_ulr']
    with pytest.raises(ValueError,match='ULR_DISTRIBUTION_OPERATOR_REQUIRED'):
        runner().apply(people[1][1],ulr_distribution_complete=True,accept_ulr_distribution=view['fingerprint'])
    assert w3.eth.get_transaction_count(operator.address)==2
    result=apply(ulr_distribution_complete=True,accept_ulr_distribution=view['fingerprint'])
    if result['status']=='CONFIRMING':w3.provider.make_request('evm_mine',[]);result=apply()
    assert result['status']=='VERIFIED'
    assert token.functions.totalSupply().call()==SUPPLY
    assert token.functions.balanceOf(owner.address).call()==SUPPLY-2*amount
    assert w3.eth.get_transaction_count(operator.address)==3
    assert apply(ulr_distribution_complete=True,accept_ulr_distribution=view['fingerprint'])['status']=='VERIFIED'
    send(w3,owner,token.functions.transfer(member.address,1))
    if depth==2:w3.provider.make_request('evm_mine',[])
    assert runner().apply(verify=True)['status']=='VERIFIED'
    assert token.functions.totalSupply().call()==SUPPLY and w3.eth.get_transaction_count(operator.address)==3
    record_property('confirmation_depth',depth)
    record_property('manual_distribution','200000 ULR to each of two members, one stakes 100000; fixed 10m supply')


def test_ulr_distribution_confirmation_is_not_reused_after_reorg(tmp_path):
    w3,people,runner,apply,first=prepare(tmp_path,1)
    operator,owner,member=[x[0] for x in people]
    b=runner();b.apply(people[0][1]);token=b.contract('dao_token')
    fork=w3.provider.make_request('evm_snapshot',[])['result']
    send(w3,owner,token.functions.transfer(member.address,200000*10**18))
    view=apply()['distribution']
    assert apply(ulr_distribution_complete=True,accept_ulr_distribution=view['fingerprint'])['status']=='VERIFIED'
    assert w3.provider.make_request('evm_revert',[fork])['result']
    w3.provider.make_request('evm_increaseTime',[7]);w3.provider.make_request('evm_mine',[])
    with pytest.raises(ValueError,match='EVM_REORG_RECOVERY_REQUIRED'):runner().apply(verify=True)

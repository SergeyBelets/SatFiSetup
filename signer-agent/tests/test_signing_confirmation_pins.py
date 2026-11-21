"""Local pins must strengthen both session reads and confirmation waits."""
import json
from types import SimpleNamespace as NS

import pytest
from web3 import Web3

from signer_agent.coordinator import CoordinatorClient
from signer_agent.participant_cli import signing_confirmations


def addr(i):return Web3.to_checksum_address(f'0x{i:040x}')


def nft(depth):return dict(version='1',chain_id='31337',confirmations=str(depth),
    board=addr(1),role='conf_ceremony',factory=addr(2),fee_manager=addr(3))


def ulr(depth):return dict(version='1',chain_id='31337',confirmations=str(depth),
    staking=addr(4),snapshots=addr(5),treasuries=[])


@pytest.mark.parametrize('enrolled,nft_depth,ulr_depth,expected',[
    (1,None,None,1),(3,1,None,3),(1,3,None,3),(1,None,4,4),(2,3,5,5),(6,3,5,6)])
def test_selected_pin_depth_can_never_reduce_invitation_depth(tmp_path,enrolled,nft_depth,ulr_depth,expected):
    paths={}
    for name,depth,make in [('nft',nft_depth,nft),('ulr',ulr_depth,ulr)]:
        if depth is not None:
            p=tmp_path/(name+'.json');p.write_text(json.dumps(make(depth)));paths[name+'_config']=p
    assert signing_confirmations(str(enrolled),**paths)==expected


def test_invalid_pin_fails_before_signing_client_configuration(tmp_path):
    p=tmp_path/'bad.json';p.write_text(json.dumps(nft(0)))
    with pytest.raises(ValueError,match='NFT_PIN_SCHEMA'):signing_confirmations('1',nft_config=p)


@pytest.mark.parametrize('chain,graph_depth,local_depth,finalized,expected',[
    (31337,1,3,None,98),(31337,5,1,None,96),(1,12,24,95,77),(1,12,12,80,80)])
def test_real_signing_snapshot_honors_stronger_depth_without_rewriting_graph(chain,graph_depth,local_depth,finalized,expected):
    def block(n):
        number=100 if n=='latest' else finalized if n=='finalized' else n
        return NS(number=number,hash=number.to_bytes(32,'big'))
    client=object.__new__(CoordinatorClient)
    client.config=NS(confirmations=local_depth)
    client.bitcoin_graph=dict(evm_confirmations=str(graph_depth),sentinel='enrolled identity')
    original=dict(client.bitcoin_graph)
    client.w3=NS(eth=NS(chain_id=chain,get_block=block))
    client.pin_read_snapshot()
    assert client._read_snapshot.number==expected
    assert client.bitcoin_graph==original


def test_durable_share_delivery_waits_for_same_strict_depth(monkeypatch):
    from signer_agent import bootstrap
    captured=[]
    class Runner:
        wire=staticmethod(bootstrap.Bootstrap.wire)
        def __init__(self,profile,state,*,evm_confirmation_floor):
            captured.append((profile,evm_confirmation_floor))
            self.journal=NS(get=lambda key:dict(result=dict(tx_hash='same-durable-tx')))
        def apply_confirmed(self,identity):return dict(status='VERIFIED')
    monkeypatch.setattr(bootstrap,'Bootstrap',Runner)
    client=object.__new__(CoordinatorClient)
    client.config=NS(confirmations=3,rpc_url='http://fixture.invalid')
    client.bitcoin_graph=dict(version='2',bitcoin_network='regtest',evm_confirmations='1')
    client.w3=NS(eth=NS(chain_id=31337,get_code=lambda address:b'canonical fixture'))
    client.check_read_snapshot=lambda:None
    client.identity_file='own-identity';client.transaction_journal='own-journal'
    assert client._send(NS(address=addr(8),fn_name='submitNonceCommitments',args=[]))=='same-durable-tx'
    profile,floor=captured[0]
    assert profile['evm_confirmations']=='1' and floor==3
    assert client.bitcoin_graph['evm_confirmations']=='1'


def test_legacy_journal_shape_is_unchanged_when_no_stricter_pin_exists():
    client=object.__new__(CoordinatorClient);client.config=NS(confirmations=1)
    client.bitcoin_graph=dict(bitcoin_network='regtest')
    assert client._confirmation_graph()==dict(bitcoin_network='regtest')


@pytest.mark.parametrize('saved_depth', ['1', '4'])
def test_confirmation_floor_preserves_durable_profile_and_checks_receipt(tmp_path, monkeypatch, saved_depth):
    from signer_agent.bootstrap import Bootstrap
    from signer_agent.operations import Journal

    def block(n):
        height=100 if n=='latest' else n
        return NS(number=height,hash=height.to_bytes(32,'big'))
    eth=NS(chain_id=31337,block_number=100,get_block=block)
    class Provider:
        HTTPProvider=staticmethod(lambda *a,**k:None)
        def __init__(self,*a):self.eth=eth
    monkeypatch.setattr('signer_agent.bootstrap.Web3',Provider)
    monkeypatch.setattr('signer_agent.bootstrap_recovery.check_receipt_binding',lambda *_:None)
    state=tmp_path/'signing.sqlite'
    profile=dict(deployment_id='signer-operation',chain_id='31337',mode='integration',
        bitcoin_network='regtest',evm_rpc='http://fixture.invalid',evm_confirmations='1')
    saved=dict(profile,evm_confirmations=saved_depth)
    journal=Journal(state);journal.put(profile['deployment_id'],dict(version='1',profile=saved))
    runner=Bootstrap(profile,state,evm_confirmation_floor=3)
    assert runner.evm_confirmations==max(3,int(saved_depth))
    receipt=NS(status=1,blockNumber=99,blockHash=block(99).hash)
    assert runner.verify_receipt({},receipt=receipt) is None
    eth.block_number=103
    assert runner.verify_receipt({},receipt=receipt) is receipt
    assert journal.get(profile['deployment_id'])['request']['profile']==saved
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):
        Bootstrap(dict(profile,steps=[{'id':'changed'}]),state,evm_confirmation_floor=3)

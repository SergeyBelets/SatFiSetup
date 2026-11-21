"""Real watch/SQLite/native descriptor; explicit registered graph and worker boundaries."""
from types import SimpleNamespace as NS
from pathlib import Path
from web3 import Web3
import pytest

from signer_agent import address_spv_watch as m
from signer_agent.zk_worker import Inbox
from test_address_watch import watched,block,tx
from test_zk_worker import config
from test_address_transport import private


@pytest.fixture
def bridge(watched,tmp_path,monkeypatch):
    e=watched
    second=tx(bytes.fromhex(e.desc['script_pubkey']),tag=4)
    e.view.chain=[e.genesis,block(e.genesis['hash'],[e.raw,second],1)]
    e.store.scan(e.bitcoin,max_blocks=2)
    where=lambda b:Web3.to_checksum_address(b)
    pool=where(e.intent.pool);reg=where(e.intent.deposit_registry)
    c=config(tmp_path);c.update(allowed_consumers={pool:'MiningPoolV1'},address_deposits=dict(version='1',registries={pool:reg}))
    private(Path(c['identity_file']),dict(private_key='11'*32))
    graph=dict(chain_id=c['chain_id'],evm_confirmations=c['evm_confirmations'],bitcoin_network='mainnet',bitcoin_policy=c['bitcoin_policy'],
        contracts=dict(relay=dict(address=c['zk_state']),deposits=dict(address=where(b'\x06'*20)),address_registry=dict(address=reg)))
    ctx=dict(pool=NS(address=pool),registry=NS(address=reg));b=NS(number=20,hash=b'h'*32)
    client=NS(store=e.store,c=dict(max_messages_per_poll='1'),context=lambda:(b,None,ctx,None,None),stable=lambda _:None,w3=NS())
    monkeypatch.setattr(m,'graph',lambda *_:graph)
    monkeypatch.setattr('signer_agent.address_mint.binding',lambda *_:dict(intent=e.intent))
    monkeypatch.setattr('signer_agent.zk_worker.load_config',lambda _:c)
    inbox=Inbox(c);sent=[]
    def request(path,graph,need):
        sent.append(need);key=inbox.enqueue(need)
        return dict(status='NEEDS_STATE',local_need_id=key)
    monkeypatch.setattr('signer_agent.zk_worker.request_from_consumer',request)
    return NS(**locals())


def test_watch_scheduler_round_robin_bounds_and_restart_keep_same_need(bridge):
    e=bridge
    first=m.sync(e.client,worker_config='explicit')
    assert first['processed']==1 and first['ready'] is False
    m.sync(e.client,worker_config='explicit');again=m.sync(e.client,worker_config='explicit')
    assert e.sent[0]==e.sent[2] and e.sent[0]['txid']!=e.sent[1]['txid']
    assert len(e.inbox.needs())==2 and again['needs'][0]['result']==first['needs'][0]['result']
    assert len(e.e.store.outputs(e.e.intent.intent_id.hex()))==2
    e.inbox.close()


def test_unregistered_watch_obligation_is_retained_without_requesting_paid_work(bridge,monkeypatch):
    e=bridge
    def unregistered(*_):raise ValueError('BTC_ADDRESS_BINDING_MISMATCH')
    monkeypatch.setattr('signer_agent.address_mint.binding',unregistered)
    result=m.sync(e.client,worker_config='explicit')
    assert result['needs'][0]['result']==dict(status='BLOCKED',reason='BTC_ADDRESS_BINDING_MISMATCH')
    assert not e.sent and len(e.e.store.outputs(e.e.intent.intent_id.hex()))==2
    e.inbox.close()


def test_watch_route_cannot_change_to_another_payer_or_worker_directory(bridge):
    e=bridge;m.sync(e.client,worker_config='explicit');e.c['directory']+='-other'
    with pytest.raises(ValueError,match='SPV_ADDRESS_ROUTE_CHANGED'):m.sync(e.client,worker_config='explicit')
    assert len(e.sent)==1;e.inbox.close()

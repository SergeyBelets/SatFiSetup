"""Real native funding/intent/HPKE/SQLite; explicit Core/EVM/qualification fixtures."""
import json
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from eth_abi import encode
from eth_utils import keccak
from web3.datastructures import AttributeDict

from signer_agent import address_spv as m,address_mint,spv_transport
from signer_agent.zk_worker import Worker,Inbox,load_config
from signer_agent.zk_worker_service import semantic_key
from signer_agent.bitcoin_network import header_hash
from signer_agent.zk_worker_prover import write
from test_address_mint import funding_env,address,Functions
from test_spv_transport import fake_client,client_config
from test_zk_worker import config


@pytest.fixture
def observed(funding_env,monkeypatch):
    e=funding_env;e.view.canonical=False;e.view.depth=6;e.view.height=10
    block=AttributeDict(dict(number=20,hash=b'h'*32,timestamp=100))
    e.w3.eth.get_block=lambda _:block
    e.pool.functions.values.update(authorization=address(21))
    e.reg_values['escrow']=address(22)
    e.common_values.update(spv=address(8),depth=lambda:e.view.depth)
    escrow=NS(address=address(22),functions=Functions(dict(spv=address(8))))
    adapter=NS(address=address(8),functions=Functions(dict(getBlockStatus=lambda _: (e.view.canonical,100))))
    verifier=NS(address=address(7),functions=Functions(dict(QUALIFIED=lambda:e.view.qualified)))
    nodes={x.address:x for x in (e.pool,e.r,escrow,adapter,verifier)}
    e.w3.eth.contract=lambda address,**_:nodes[address]
    monkeypatch.setattr(address_mint,'configuration',lambda *_:e.context)
    original=e.bitcoin.call
    def header():return b'\x01\0\0\0'+bytes(32)+bytes.fromhex(e.view.txid)[::-1]+bytes(12)
    def rpc(method,*args):
        if method=='getrawtransaction':return dict(hex=e.view.raw.hex(),blockhash=header_hash(header())[::-1].hex())
        if method=='getblockheader':return dict(height=10) if args[1] else header().hex()
        if method=='getblockhash':
            if args[0]==0:return e.intent.bitcoin_genesis.hex()
            return header_hash(header())[::-1].hex() if args[0]==10 else 'aa'*32
        return original(method,*args)
    e.bitcoin.call=rpc
    monkeypatch.setattr('signer_agent.bitcoin_proofs.inclusion',lambda *_:dict(block_hash=header_hash(header()).hex(),siblings=[],index='0'))
    w=Worker.__new__(Worker);w.w3=e.w3;w.bitcoin=e.bitcoin
    profile=dict(version='1',registries={e.pool.address:e.r.address})
    w.c=dict(bitcoin_policy=dict(checkpoint_height='0'),allowed_consumers={e.pool.address:'MiningPoolV1'},address_deposits=profile)
    w.contract=lambda a,kind,b:nodes[a]
    need=dict(kind='address_deposit',consumer=e.pool.address,intent_id=e.intent.intent_id.hex(),txid=e.view.txid,vout='0')
    core=dict(blocks=109,bestblockhash='aa'*32)
    def observe():
        need['txid']=e.view.txid
        return w.observe_need(need,block,adapter,core)
    return NS(**locals())


def test_registered_output_is_distinct_from_base_pool_script_and_validates_merkle(observed):
    o=observed
    assert o.observe()['status']=='NEEDS_STATE'
    o.e.view.canonical=True
    assert o.observe()['status']=='READY'
    assert 'payoutScript' not in o.e.pool.functions.values
    o.monkeypatch.setattr('signer_agent.bitcoin_proofs.inclusion',lambda *_:dict(block_hash='00'*32,siblings=['00'*32],index='0'))
    with pytest.raises(ValueError,match='ZK_NEED_INCLUSION'):o.observe()


@pytest.mark.parametrize('coinbase,threshold',[(False,6),(True,100)])
def test_actual_native_transaction_maturity_before_any_state_job(observed,coinbase,threshold):
    o=observed;o.e.transaction(coinbase=coinbase);o.e.view.confirmations=threshold-1
    assert o.observe()['status']=='WAITING_BITCOIN'
    o.e.view.confirmations=threshold
    assert o.observe()['depth']==threshold


@pytest.mark.parametrize('field,value,code',[('qualified',False,'BTC_ADDRESS_VERIFIER_UNQUALIFIED'),
    ('custody',2,'BTC_MINT_GENERATION_CHANGED'),('derivation',1,'BTC_MINT_GENERATION_CHANGED'),('spent',True,'BTC_MINT_OUTPOINT_UNAVAILABLE')])
def test_pending_worker_need_checks_current_permission_and_real_utxo(observed,field,value,code):
    setattr(observed.e.view,field,value)
    with pytest.raises(ValueError,match=code):observed.observe()


def test_drain_close_issuance_does_not_cancel_accepted_mint_spv_right(observed):
    o=observed;o.e.view.custody=1
    assert o.observe()['status']=='NEEDS_STATE'


def test_unknown_binding_or_foreign_registry_cannot_enter_paid_worker(observed,monkeypatch):
    o=observed;o.w.c['address_deposits']['registries'][o.e.pool.address]=address(99)
    with pytest.raises(ValueError,match='SPV_ADDRESS_REGISTRY_MISMATCH'):o.observe()
    o.w.c['address_deposits']['registries'][o.e.pool.address]=o.e.r.address
    o.need['intent_id']='00'*32
    with pytest.raises(ValueError,match='BTC_ADDRESS_BINDING_MISMATCH'):o.observe()


def test_exact_mint_receipt_recovers_before_core_and_current_revocation(observed):
    o=observed;o.e.view.minted=1000;o.e.view.qualified=False;o.e.view.custody=3;o.e.view.derivation=2
    o.e.view.consumed.add(keccak(encode(['bytes32','uint32'],[bytes.fromhex(o.e.view.txid)[::-1],0])))
    o.e.bitcoin.call=lambda *_:pytest.fail('exact completed mint must not request Core or paid work')
    assert o.observe()==dict(status='COMPLETED')


def test_consumer_backend_must_match_selected_zk_state(observed):
    o=observed;o.e.common_values['spv']=address(100)
    with pytest.raises(ValueError,match='ZK_CONSUMER_STATE_MISMATCH'):o.observe()


def test_generation_change_does_not_change_need_or_service_semantic_key(observed,tmp_path):
    o=observed;c=config(tmp_path);c.update(address_deposits=o.profile)
    w=NS(c=c,inbox=Inbox(c))
    key=w.inbox.enqueue(o.need);semantic=semantic_key(w,[key])
    o.e.view.cg+=2;o.e.view.dg+=2
    assert o.observe()['status']=='NEEDS_STATE'
    assert w.inbox.enqueue(o.need)==key and semantic_key(w,[key])==semantic
    w.inbox.close();w.inbox=Inbox(c)
    assert w.inbox.enqueue(o.need)==key
    w.inbox.close()


def test_generation_race_between_confirmed_funding_and_latest_preflight_is_fenced(observed,monkeypatch):
    original=address_mint.funding
    def raced(*args,**kwargs):
        claim=original(*args,**kwargs);observed.e.view.cg+=1;return claim
    monkeypatch.setattr(address_mint,'funding',raced)
    with pytest.raises(ValueError,match='BTC_MINT_GENERATION_CHANGED'):observed.observe()


def test_legacy_profile_rejects_address_need_instead_of_falling_back(observed):
    o=observed;o.w.c.pop('address_deposits')
    with pytest.raises(ValueError,match='BTC_ADDRESS_ZK_WORKER_PROFILE_REQUIRED'):o.observe()


def test_worker_v5_profile_is_explicit_and_upgrade_keeps_prior_scope_and_journals(tmp_path):
    c=config(tmp_path);template=json.loads(__import__('pathlib').Path('config/zkspv-worker.example.json').read_text())
    for k in ('service','heartbeat','transport','batching','archive','automation'):c[k]=template[k]
    c.update(version='4',transport=None,archive=dict(directory=str(tmp_path/'archive'),max_headers='200000',max_bytes='1073741824'))
    path=tmp_path/'config.json';write(path,c);assert load_config(path)==c
    inbox=Inbox(c);key=inbox.enqueue(dict(kind='deposit',consumer='legacy',txid='11'*32,vout='0'));inbox.reserve('existing',5)
    scope=inbox.get('scope');inbox.close()
    pool=next(iter(c['allowed_consumers']));c.update(version='5',address_deposits=dict(version='1',registries={pool:address(4)}))
    write(path,c);assert load_config(path)==c
    inbox=Inbox(c);assert inbox.get('scope')==scope and inbox.needs()[0][0]==key
    assert inbox.db.execute('SELECT cost FROM reservations').fetchone()[0]=='5';inbox.close()
    changed=deepcopy(c);changed['address_deposits']['registries'][pool]=address(5)
    with pytest.raises(ValueError,match='SPV_ADDRESS_PROFILE_CHANGED'):Inbox(changed)
    c.pop('address_deposits');c['version']='4'
    with pytest.raises(ValueError,match='SPV_ADDRESS_PROFILE_CHANGED'):Inbox(c)


def test_transport_v3_encrypted_address_need_reuses_exact_outbox(tmp_path):
    c,recipient,peer,b,board=fake_client(tmp_path)
    pool=address(88);c.c.update(version='3',address_deposits=dict(version='1',registries={pool:address(4)}))
    need=dict(kind='address_deposit',consumer=pool,intent_id='aa'*32,txid='bb'*32,vout='0')
    first=c.send_request(need,'fed',recipient)
    assert c.send_request(need,'fed',recipient)==first
    body=spv_transport.open_packet('ab'*32,first[1]['header'],first[1]['ciphertext'],peer['secret'])
    assert body['reason']=='DEPOSIT' and body['need']==need
    need['intent_id']='cc'*32
    assert c.send_request(need,'fed',recipient)[0]!=first[0]
    c.close()


def test_transport_v3_adds_pinned_profile_without_resetting_outbox_or_costs(tmp_path):
    c,recipient,peer,b,board=fake_client(tmp_path)
    old=c.store.get('scope');c.store.reserve('old',10)
    cfg=dict(c.c,version='3',address_deposits=dict(version='1',registries={address(88):address(4)}))
    path=tmp_path/'transport.json';write(path,cfg)
    assert spv_transport.load_config(path)==cfg
    who=c.account.address;c.close();store=spv_transport.Store(cfg,who)
    assert store.get('scope')==old and store.db.execute('SELECT cost FROM costs').fetchone()[0]=='10'
    store.close();cfg['address_deposits']['registries'][address(88)]=address(5)
    with pytest.raises(ValueError,match='SPV_ADDRESS_PROFILE_CHANGED'):spv_transport.Store(cfg,who)


@pytest.mark.parametrize('profile',[None,[],dict(version='1',registries={})])
def test_transport_rejects_malformed_explicit_address_profile(tmp_path,profile):
    c,_=client_config(tmp_path);c.update(version='3',address_deposits=profile)
    path=tmp_path/'bad-profile.json';write(path,c)
    with pytest.raises(ValueError,match='SPV_ADDRESS_PROFILE'):spv_transport.load_config(path)

"""Native funding/Merkle worker path; graph, Core and qualification are fixtures."""
from types import SimpleNamespace as NS
from copy import deepcopy
import json,pytest
from signer_agent import withdrawal_fee_spv as m
from signer_agent.withdrawal_fee_intent import WithdrawalFeeIntentV1
from signer_agent.funding_envelope import inspect_funding
from signer_agent.zk_worker import Inbox,load_config
from signer_agent.zk_worker_prover import write
from test_address_spv import observed
from test_address_mint import funding_env,address,Functions,VECTOR
from test_zk_worker import config

@pytest.fixture
def fee_observed(observed,monkeypatch):
    o=observed;e=o.e;intent=WithdrawalFeeIntentV1(e.intent,b'f'*32,b'h'*32,b's'*32)
    desc=intent.derive(bytes.fromhex(VECTOR['base_group']));e.b['script']=bytes.fromhex(desc['script_pubkey']);e.transaction()
    e.view.internal=False;e.view.classified=True;e.view.fee_done=False
    v=NS(functions=Functions(dict(FEE_QUALIFIED=lambda:e.view.qualified)))
    f=NS(functions=Functions(dict(classified=lambda _:len(inspect_funding(e.view.raw.hex(),e.view.txid)['inputs']) if e.view.classified else 0)))
    e.common_values['custodyTransactions']=lambda _:e.view.internal
    old=NS(address=e.r.address,functions=Functions(dict(e.reg_values,custodyGeneration=1,derivationGeneration=2)))
    latest=NS(address=e.r.address,functions=Functions(e.reg_values))
    ctx=dict(registry=old,common=e.common,escrow=o.escrow,block=o.block,withdrawal_fees=dict(verifier=v,funding=f))
    def view(w3,policy,need,number):return dict(ctx,registry=latest if number=='latest' else old),intent,desc,e.view.fee_done
    monkeypatch.setattr(m,'view',view)
    o.w.c['withdrawal_fees']=dict(version='1',registries={e.pool.address:address(90)})
    o.need.update(kind='withdrawal_fee',intent_id=intent.intent_id.hex())
    return o

def test_fee_need_native_merkle_inclusion_reuses_same_worker_state(fee_observed):
    o=fee_observed;assert o.observe()['status']=='NEEDS_STATE'
    o.e.view.canonical=True;assert o.observe()['status']=='READY'

@pytest.mark.parametrize('coinbase,configured,threshold',[(False,6,6),(True,6,101),(True,144,144)])
def test_fee_need_uses_configured_depth_and_coinbase_floor(fee_observed,coinbase,configured,threshold):
    o=fee_observed;o.e.common_values['depth']=configured;o.e.transaction(coinbase=coinbase);o.core['blocks']=10+threshold-2
    assert o.observe()['status']=='WAITING_BITCOIN'
    o.core['blocks']+=1;assert o.observe()['depth']==threshold

@pytest.mark.parametrize('field,value,code',[('internal',True,'INGRESS_CLASSIFICATION'),('classified',False,'INGRESS_CLASSIFICATION'),
    ('qualified',False,'VERIFIER_UNQUALIFIED'),('custody',2,'VERIFIER_UNQUALIFIED'),('derivation',1,'VERIFIER_UNQUALIFIED'),('cg',2,'GENERATION')])
def test_fee_need_rejects_internal_unclassified_unqualified_and_generation_races(fee_observed,field,value,code):
    setattr(fee_observed.e.view,field,value)
    with pytest.raises(ValueError,match=code):fee_observed.observe()

def test_exact_fee_receipt_completes_before_core_current_fences_or_extra_payment(fee_observed):
    o=fee_observed;o.e.view.fee_done=True;o.e.view.qualified=False;o.e.view.custody=3
    o.e.bitcoin.call=lambda *_:pytest.fail('completed receipt must not schedule more paid work')
    assert o.observe()==dict(status='COMPLETED')

def test_fee_worker_rejects_wrong_adapter_in_same_public_need(fee_observed):
    o=fee_observed;o.e.common_values['spv']=address(99)
    with pytest.raises(ValueError,match='ZK_CONSUMER_STATE_MISMATCH'):o.observe()

def test_fee_need_scope_is_distinct_from_deposit_and_requires_explicit_root_pin():
    need=dict(kind='withdrawal_fee',consumer=address(1),intent_id='11'*32,txid='22'*32,vout='0')
    p=dict(allowed_consumers={address(1):'MiningPoolV1'},withdrawal_fees=dict(version='1',registries={address(1):address(2)}))
    assert m.scope(p,need)==address(2)
    with pytest.raises(ValueError):m.scope(dict(p,withdrawal_fees=None),need)
    with pytest.raises(ValueError):m.scope(p,dict(need,kind='address_deposit'))
    with pytest.raises(ValueError):m.check_graph(p,dict(contracts=dict(withdrawal_credits=dict(address=address(3)))),need)
    m.check_graph(p,dict(contracts=dict(withdrawal_credits=dict(address=address(2)))),need)

def test_worker_v6_preserves_queue_reservations_and_refuses_profile_rollback(tmp_path):
    c=config(tmp_path);template=json.loads(__import__('pathlib').Path('config/zkspv-worker.example.json').read_text())
    for k in ('service','heartbeat','transport','batching','archive','automation'):c[k]=template[k]
    pool=next(iter(c['allowed_consumers']));c.update(version='5',transport=None,
        archive=dict(directory=str(tmp_path/'archive'),max_headers='200000',max_bytes='1073741824'),
        address_deposits=dict(version='1',registries={pool:address(4)}))
    inbox=Inbox(c);key=inbox.enqueue(dict(kind='deposit',consumer=pool,txid='11'*32,vout='0'));inbox.reserve('existing',5);saved=inbox.get('scope');inbox.close()
    c.update(version='6',withdrawal_fees=dict(version='1',registries={pool:address(5)}));path=tmp_path/'config.json';write(path,c)
    assert load_config(path)==c
    inbox=Inbox(c);assert inbox.get('scope')==saved and inbox.needs()[0][0]==key
    assert inbox.db.execute('SELECT cost FROM reservations').fetchone()[0]=='5';inbox.close()
    changed=deepcopy(c);changed['withdrawal_fees']['registries'][pool]=address(6)
    with pytest.raises(ValueError,match='SPV_WITHDRAWAL_FEE_PROFILE_CHANGED'):Inbox(changed)
    c.pop('withdrawal_fees');c['version']='5'
    with pytest.raises(ValueError,match='SPV_WITHDRAWAL_FEE_PROFILE_CHANGED'):Inbox(c)

@pytest.mark.parametrize('status',['READY','COMPLETED','COVERED_BY_STATE','WAITING_BITCOIN'])
def test_no_extra_admission_payment_when_state_already_covers_fee_need(monkeypatch,status):
    from signer_agent import spv_automation
    monkeypatch.setattr(spv_automation,'observation',lambda *_:(dict(status=status),{}))
    guard=m.guard(None,{},None,dict(kind='withdrawal_fee'),None,{})
    with pytest.raises(ValueError,match='WORK_NOT_REQUIRED'):guard()

"""Real native purpose/key/FROST and SQLite; EVM/Core boundaries are fixtures."""
from copy import deepcopy
from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path
import json,pytest
from eth_utils import keccak
from signer_agent import withdrawal_fee as m,bitcoin_input_keys as keys,bitcoin_worker as worker
from signer_agent.withdrawal_fee_intent import WithdrawalFeeIntentV1,PROFILE
from signer_agent.withdrawal_fee_watch import FeeWatch
from signer_agent.withdrawal_fee_transport import publish_intent,publish_plan
from signer_agent.bitcoin_onchain_plan import validate_plan,serialize_plan,decode_plan
from signer_agent.deposit_intent import DepositIntentV1
from test_bitcoin_input_keys import signing,native_keys,test_mixed_plan_roundtrip_build_and_native_signatures_share_the_existing_journal as sign_real
from test_address_mint import Functions,address
from test_address_registration import registered_env,custody_profile
from test_address_transport import delivery


@pytest.mark.parametrize('foreign_creator',[False,True])
def test_withdrawal_fee_review_accepts_delegated_creator_without_execution(registered_env,monkeypatch,foreign_creator):
    """Isolate session review; monetary operation construction is an explicit fixture."""
    from signer_agent import withdrawal_fee_policy as policy
    from signer_agent.operations import canonical
    e=registered_env;custody_profile(e);op=b'o'*32
    ctx=dict(e.ctx,block=e.block(20));inspector=SimpleNamespace(c=dict(reserve=address(60)),context=lambda _:ctx,stable=e.service.stable)
    monkeypatch.setattr(policy,'operation',lambda *args:(e.reg,'WithdrawalFeePayments','getPrepareOperationHash',[],[0,0,0,1600],op))
    e.auth.functions.values['executedOperations']=lambda _:False
    e.sessions[8]=[8,address(100) if foreign_creator else e.service.account.address,b'g'*32,op,True,2,2,1600,True,m.ZERO,1,0,0,0,0,e.reg.address]
    intent=dict(chain_id='31337',role='conf_ceremony',coordinator=e.coordinator.address,
        target=e.reg.address,artifact='WithdrawalFeePayments',hash_method='getPrepareOperationHash',hash_args=[],
        operation_hash=op.hex(),session='8',withdrawal_fee_request=dict(version='1',pool=e.pool.address,
            registration_board=e.registration_board.address,reserve=address(60),method='prepare',args=[],bitcoin_plan=None))
    e.public[8]=canonical(intent).encode()
    if foreign_creator:
        with pytest.raises(ValueError,match='WITHDRAWAL_FEE_SESSION_MISMATCH'):
            policy.review(e.w3,intent,e.bundle,20,inspector=inspector,executable=False)
    else:
        result=policy.review(e.w3,intent,e.bundle,20,inspector=inspector,executable=False)
        assert result['session']==8 and not result['complete']
    assert not e.approved and not e.sent

@pytest.fixture
def personal(signing,monkeypatch):
    e=signing;parent=DepositIntentV1.decode(bytes.fromhex(e.plan['input_keys'][2]['intent']))
    intent=WithdrawalFeeIntentV1(parent,b'f'*32,b'h'*32,b's'*32);desc=intent.derive(bytes.fromhex(e.key['result']['group']))
    e.plan.update(version='4',withdrawal_fee=dict(version='1',payments=address(30),previous='00'*32,treasury='00'*32,
        charges=[['0x'+'66'*32,'1','1000']],credits=[]))
    e.plan['inputs'][2]['script']=desc['script_pubkey'];e.plan['input_keys'][2]=desc['input_key'];e.plan['input_provenance'][2]='aa'*32
    e.graph['withdrawal_fees']=dict(profile='WITHDRAWAL_FEE_V1');e.v.fee_qualified=True
    # Monetary admission is covered by the Solidity suite. This native-key
    # fixture models a BTC fee source (no token fee burn), never MP/S funding.
    e.escrow.w3=e.w3;e.escrow.functions.values['withdrawalFees']=address(33)
    e.nodes[address(33)]=SimpleNamespace(address=address(33),functions=Functions(dict(getBudget=lambda _:[address(1),address(13),address(0),1])))
    e.common_values['feeScripts']=lambda h:intent.intent_id if h==keccak(bytes.fromhex(desc['script_pubkey'])) else bytes(32)
    f=SimpleNamespace(address=address(31),functions=Functions(dict(verifierRuntimeHash=e.runtime_hash)))
    v=SimpleNamespace(address=address(32),functions=Functions(dict(FEE_QUALIFIED=lambda:e.v.fee_qualified,FEE_PROFILE=keccak(text=PROFILE))))
    monkeypatch.setattr(m,'for_graph',lambda *_:dict(withdrawal_fees=dict(funding=f,verifier=v)))
    monkeypatch.setattr(m,'funding_binding',lambda *a:(intent,desc,bytes.fromhex('aa'*32)))
    return e,intent,desc

def test_fee_purpose_mixed_signatures_use_real_native_crypto_and_same_spent_journal(personal):
    e,_,_=personal;sign_real(e)

def test_fee_purpose_cannot_borrow_deposit_qualification_or_export_cached_nonce(personal):
    e,_,_=personal
    committed=worker.sign_step('btc-commit',e.manifest,e.key,e.plan,e.registry,e.states[0],e.bitcoin)
    sid='btc:'+e.manifest.context.hex()+':'+e.plan['batch_id']+':0';before=e.states[0].get(sid)
    e.v.fee_qualified=False
    for action in ('btc-commit','btc-sign'):
        with pytest.raises(ValueError,match='WITHDRAWAL_FEE_VERIFIER_UNQUALIFIED'):
            worker.sign_step(action,e.manifest,e.key,e.plan,e.registry,e.states[0],e.bitcoin)
    assert e.states[0].get(sid)==before and committed['values']

def test_fee_and_deposit_domains_and_refund_scopes_are_distinct(personal):
    e,intent,desc=personal;group=bytes.fromhex(e.key['result']['group'])
    assert len(intent.encode())==504 and WithdrawalFeeIntentV1.decode(intent.encode())==intent
    assert desc['script_pubkey']!=intent.parent.derive(group)['script_pubkey']
    for modified in (replace(intent,request=b'x'*32),replace(intent,refund_script_hash=b'x'*32),replace(intent,credits_scope=b'x'*32)):
        result=modified.derive(group)
        assert result['script_pubkey']!=desc['script_pubkey'] and result['binding_commitment']!=desc['binding_commitment']
        assert result['wallet_context']==desc['wallet_context'] and result['ready'] is False
    assert desc['input_key']['mode']=='WITHDRAWAL_FEE'

@pytest.mark.parametrize('field,value',[('previous','01'*32),('treasury','01'*32),('charges',[['0x'+'66'*32,'1','999']]),('credits',[['0x'+'44'*32,'0x'+'66'*32,'1','0','1']])])
def test_exact_public_plan_hash_commits_fee_terms(personal,field,value):
    from signer_agent.bitcoin_onchain_plan import onchain_plan_hash
    e,_,_=personal;p=deepcopy(e.plan);p['withdrawal_fee'][field]=value
    assert onchain_plan_hash(e.manifest,p)!=onchain_plan_hash(e.manifest,e.plan)

@pytest.mark.parametrize('mutate',[lambda p:p.update(version='3'),lambda p:p['input_keys'][2].update(mode='DERIVED'),
    lambda p:p['input_keys'][2].update(intent='00'*504),lambda p:p['withdrawal_fee']['charges'][0].__setitem__(1,'01'),
    lambda p:p['withdrawal_fee'].update(credits=[['0x'+'ab'*32,'0x'+'66'*32,'1','0','1']]*2)])
def test_noncanonical_or_wrong_purpose_plan_rejected_before_private_work(personal,mutate):
    e,_,_=personal;p=deepcopy(e.plan);mutate(p)
    with pytest.raises((ValueError,RuntimeError)):worker.sign_step('btc-commit',e.manifest,e.key,p,e.registry,e.states[0],e.bitcoin)
    assert e.states[0].get('btc:'+e.manifest.context.hex()+':'+p['batch_id']+':0') is None

def test_full_16_input_fee_plan_is_bounded_and_round_trips(personal):
    e,_,_=personal;p=deepcopy(e.plan);source=deepcopy(p['inputs'][2]);p['inputs']=[];p['input_keys']=[];p['input_provenance']=[]
    for index in range(16):
        p['inputs'].append(dict(source,outpoint=('%064x'%(index+1))+':0'))
        p['input_keys'].append(deepcopy(e.plan['input_keys'][2]));p['input_provenance'].append('aa'*32)
    p['outputs'][1]['amount']='154000';p['withdrawal_refs']=['%064x'%(i+1) for i in range(32)]
    p['withdrawal_fee']['charges']=[['0x'+r,'1','0'] for r in p['withdrawal_refs']]
    p['withdrawal_fee']['credits']=[['0x'+'%064x'%(i+1),'0x'+'%064x'%(i+1),'0','0','1'] for i in range(32)]
    raw=serialize_plan(p,e.manifest)
    assert 16384<len(raw)<=65536 and decode_plan(raw,e.manifest)==p
    assert len(keys.build(p)['sighashes'])==16

def test_fee_watch_is_durable_separate_and_restores_exact_descriptor(personal,tmp_path):
    e,intent,desc=personal
    scope=dict(purpose='WITHDRAWAL_FEE',bitcoin_genesis=intent.parent.bitcoin_genesis.hex(),bitcoin_network='regtest',
        ledger_scope=intent.credits_scope.hex(),base_group=e.key['result']['group'])
    s=FeeWatch(tmp_path/'fee.sqlite',scope);receipt=s.accept(intent.encode(),bytes.fromhex(scope['base_group']),'regtest','route',dict(binding='aa'*32))
    assert receipt['status']=='DURABLE' and receipt['ready'] is False
    assert s.accept(intent.encode(),bytes.fromhex(scope['base_group']),'regtest','route',dict(binding='aa'*32))==receipt
    backup=tmp_path/'backup.sqlite';s.backup(backup);restored=FeeWatch(backup,scope)
    assert restored.intents()==s.intents();restored._check_immutable(restored.intents()[0][1])
    with pytest.raises(ValueError,match='WITHDRAWAL_FEE_WATCH_SCOPE'):
        s.accept(replace(intent,credits_scope=b'z'*32).encode(),bytes.fromhex(scope['base_group']),'regtest','bad',{})

class PublicBoard:
    def __init__(self):
        self.full=b'';self.partial=b'';self.calls=[];self.address=address(90)
        self.functions=Functions(dict(intents=lambda _:self.full,feeIntentPartial=lambda _:self.partial,
            plan=lambda _:self.full,feePlanPartial=lambda *_:self.partial))
    def send(self,method,args,part):
        self.calls.append((method,args,part))
        if method.startswith('append'):
            _,total,index,chunk=args
            assert index*8192==len(self.partial) and 0<len(chunk)<=8192
            self.partial+=chunk;assert len(self.partial)<=total
        else:self.full=self.partial

@pytest.mark.parametrize('kind,size',[('intent',131072),('plan',65536)])
def test_large_public_transport_resumes_after_partial_and_does_not_republish(kind,size):
    b=PublicBoard();raw=b'x'*size;b.partial=raw[:16384]
    call=lambda:publish_intent(b,1,raw,b.send) if kind=='intent' else publish_plan(b,b'a'*32,address(1),raw,b.send)
    call();assert b.full==raw and len(b.calls)==size//8192-1
    before=list(b.calls);call();assert before==b.calls
    b.full=b'bad'
    with pytest.raises(ValueError):call()

@pytest.mark.parametrize('partial',[b'bad',b'x',b'x'*8193])
def test_corrupt_public_partial_cannot_be_completed_or_signed(partial):
    b=PublicBoard();b.partial=partial
    with pytest.raises(ValueError):publish_intent(b,1,b'x'*20000,b.send)
    assert not b.full and not b.calls

def test_typed_action_rejects_arbitrary_method_without_hash_helper():
    from signer_agent.withdrawal_fee_policy import operation
    with pytest.raises(ValueError,match='WITHDRAWAL_FEE_ACTION'):operation({},'transfer',[],None)

@pytest.mark.parametrize('wrong_address,wrong_args',[(False,False),(True,False),(False,True)])
def test_fixed_reserve_child_requires_canonical_parent_constructor_and_nonce1(monkeypatch,wrong_address,wrong_args):
    import rlp
    from signer_agent import contract_provenance as p,bitcoin_trust,verified_cache
    parent=address(60)
    child=address(61) if wrong_address else p.Web3.to_checksum_address(keccak(rlp.encode([bytes.fromhex(parent[2:]),1]))[-20:])
    node=SimpleNamespace(functions=Functions(dict(reserve=parent)))
    w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda **_:node,get_block=lambda n:dict(hash=b'h'*32)))
    proof=dict(version='1',address=parent,artifact='BitcoinFeeReserve',method='direct',block_number='1',block_hash=(b'h'*32).hex(),
        transaction_hash='0x'+'ab'*32,runtime_hash='aa'*32)
    monkeypatch.setattr(bitcoin_trust,'checked_runtime',lambda *a:'aa'*32)
    monkeypatch.setattr(verified_cache,'get',lambda *a:None);monkeypatch.setattr(verified_cache,'put',lambda *a:None)
    original=p.prove_creation;parents=[]
    def invoke(w3,where,kind,args,latest,**kwargs):
        if kind=='BitcoinFeeReserve':parents.append(where);assert where==parent;return proof
        return original(w3,where,kind,args,latest,**kwargs)
    monkeypatch.setattr(p,'prove_creation',invoke)
    if wrong_address or wrong_args:
        with pytest.raises(ValueError,match='BTC_CONSTRUCTOR_INPUT_MISMATCH'):invoke(w3,child,'BitcoinFeeReserveUser',[address(9) if wrong_args else parent],10)
        assert not parents
    else:
        result=invoke(w3,child,'BitcoinFeeReserveUser',[parent],10)
        assert result['method']=='canonical_constructor' and result['parent']==proof and parents==[parent]

def test_total_mp_preview_keeps_principal_and_subsat_rounding_separate():
    from signer_agent.withdrawal_fee_cli import preview
    i=SimpleNamespace(context=lambda:dict(block=SimpleNamespace(number=1)))
    p=preview(i,'MP',str(600*10**10+99),'100')
    assert p['principal_sats']=='500' and p['fee_tranche_units']==str(100*10**10) and p['rounding_units']=='99'

def test_s_total_preview_applies_ordinary_fee_only_to_remaining_main_tranche():
    from signer_agent.withdrawal_fee_cli import preview
    calls=[]
    s=SimpleNamespace(functions=Functions(dict(feeManager=address(2))))
    f=SimpleNamespace(functions=Functions(dict(calculateFee=lambda direction,units:calls.append((direction,units)) or 13*10**10)))
    i=SimpleNamespace(context=lambda:dict(block=SimpleNamespace(number=1)),w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda address,**_:s if address==__import__('test_address_mint').address(1) else f)))
    p=preview(i,'S',str(1000*10**10),'100',pool=address(1))
    assert calls==[(False,900*10**10)] and p['principal_sats']=='887' and p['ordinary_service_fee_units']==str(13*10**10)


def test_nft_wallet_preview_uses_read_only_quote_not_quota_consumption(monkeypatch):
    from signer_agent import withdrawal_fee_cli as cli
    calls=[];pool=SimpleNamespace(address=address(1));manager=SimpleNamespace(address=address(2))
    def read(target,block,method,*args):
        if method=='feeManager':return manager.address
        assert method=='quoteFeeWithNFT', 'wallet preview must never invoke a consuming method'
        calls.append(args);return 13*10**10
    monkeypatch.setattr(cli,'read',read)
    monkeypatch.setattr('signer_agent.spv_automation.identity',lambda _:address(3))
    i=SimpleNamespace(c=dict(identity_file='local-only'),context=lambda:dict(block=SimpleNamespace(number=1)),
        w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda address,**_:pool if address==pool.address else manager)))
    result=cli.preview(i,'S',str(1000*10**10),'100',pool=pool.address,nft='7')
    assert calls==[(False,900*10**10,7,address(3))]
    assert result['principal_sats']=='887'

@pytest.mark.parametrize('confirmations,expected',[(5,'BTC_IMMATURE'),(6,'INPUTS_AND_EXACT_VERSION_REQUIRED'),(None,'BTC_UNKNOWN')])
def test_status_keeps_named_rights_when_utxo_is_unknown_and_never_calls_lock_funded(confirmations,expected):
    from signer_agent.withdrawal_fee_cli import status
    request=b'r'*32;location=b'l'*32
    budget=[address(1),address(2),address(0),1,1,0,100,10,100,999,b'h'*32,bytes(32),bytes.fromhex('0014'+'55'*20),False]
    credit=SimpleNamespace(functions=Functions(dict(getBudget=lambda _:budget,refundOrigin=lambda _:bytes(32),claimCount=1,
        claimAt=lambda _:(b'i'*32,[request,location,1000,bytes(32)]),refundCommitted=lambda _:0,refundSpent=lambda _:0)))
    payments=SimpleNamespace(functions=Functions(dict(rootForRequest=lambda _:bytes(32))))
    common=SimpleNamespace(functions=Functions(dict(valueLocation=lambda _:(b't'*32,0),depth=6)))
    escrow=SimpleNamespace(functions=Functions(dict(getRequest=lambda _:[address(0),0,address(2),address(1),500*10**10,b'',bytes(32),1])))
    verifier=SimpleNamespace(functions=Functions(dict(FEE_QUALIFIED=False)))
    i=SimpleNamespace(context=lambda:dict(block=SimpleNamespace(number=1),common=common,escrow=escrow,
        withdrawal_fees=dict(credits=credit,payments=payments,verifier=verifier)),_bounded=lambda _:None,
        observed=lambda *_:(None,None) if confirmations is None else (dict(coinbase=False,confirmations=confirmations),None))
    p=status(i,request)
    assert p['status']==expected and p['btc_credit_sats']=='1000' and p['btc_available_sats']=='1000'
    assert p['principal_sats']=='500' and p['blocked_by']=='FEE_PURPOSE_UNQUALIFIED' and p['ready'] is False

@pytest.mark.parametrize('tamper,closed',[(False,False),(True,False),(False,True)])
def test_owner_quote_binds_entire_candidate_and_keeps_fixed_cost_after_free_cap_raise(tamper,closed):
    from signer_agent.withdrawal_fee_client import quote
    from signer_agent.bootstrap import artifact
    from eth_abi import encode
    from eth_utils.abi import collapse_if_tuple
    request=b'r'*32;batch=b'b'*32;scope=b's'*32
    candidate=[(0,1,2,1000),batch,bytes(32),b'p'*32,b'public raw Bitcoin fixture',[request],[(request,1,10)],[],bytes(32)]
    spec=next(x for x in artifact('WithdrawalFeePayments')['abi'] if x.get('name')=='getPrepareOperationHash')['inputs'][0]
    digest=keccak(encode(['bytes32',collapse_if_tuple(spec)],[scope,candidate]))
    if tamper:candidate[4]=b'other Bitcoin transaction'
    verified=[batch,b't'*32,b'i'*32,10,10,0,0,2,[]]
    p=SimpleNamespace(functions=Functions(dict(candidate=lambda _:candidate,verification=lambda _:verified,scope=scope,termsHash=lambda _:digest,
        bound=lambda _:True,settled=lambda _:closed,winner=lambda _:batch if closed else bytes(32))))
    budget=[address(1),address(2),address(0),2,2,0 if closed else 200*10**10,200,10,100,999,b'h'*32,batch,b'',closed]
    c=SimpleNamespace(functions=Functions(dict(getBudget=lambda _:budget)))
    escrow=SimpleNamespace(functions=Functions(dict(getRequest=lambda _:[address(0),0,address(2),address(1),500*10**10,b'',batch,3 if closed else 2])))
    i=SimpleNamespace(context=lambda:dict(block=SimpleNamespace(number=1),escrow=escrow,withdrawal_fees=dict(credits=c,payments=p)))
    if tamper:
        with pytest.raises(ValueError,match='WITHDRAWAL_FEE_QUOTE_BINDING'):quote(i,batch,request)
    else:
        result=quote(i,batch,request)
        assert result['owner_cost_sats']=='10' and result['revision']=='1' and result['current_budget_revision']=='2'
        assert result['principal_sats']=='500' and result['current_cap_sats']=='200'
        assert result['expected_token_return_units']==(None if closed else str(190*10**10))

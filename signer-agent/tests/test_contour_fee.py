"""Original Owner/EOA/budget/ABI path; current contract effects and graph are explicit fixtures."""
from copy import deepcopy
from types import SimpleNamespace as NS
from pathlib import Path
import json

import pytest
from eth_account import Account
from eth_abi import encode
from eth_utils import keccak

from ops.zkspv import contour_fee as fee
from ops.zkspv.contour import digest
from ops.zkspv.contour_calls import Calls,Progress
from signer_agent.withdrawal_fee_client import Owner
from signer_agent.operations import Journal
from signer_agent.zk_worker_prover import write
from signer_agent.withdrawal_fee_funding import Funding
from test_contour_withdrawal import envelopes,approve
from test_address_mint import address,Functions,funding_env
from test_zk_worker import config


@pytest.fixture(params=['REFUND_MP','REFUND_S','CREATE_BTC_REFUND'])
def refund(envelopes,request,monkeypatch):
    e=envelopes;mode=request.param;i=e.actors['owner'];owner=Account.from_key((1).to_bytes(32,'big')).address
    old='11'*32;new='22'*32;scope=b's'*32;nonce=0;script='0014'+'33'*20
    args=['0x'+'44'*32,'0x'+old,'1000','100','10','1000','1','1']
    if mode=='CREATE_BTC_REFUND':new=keccak(encode(['bytes32','address','uint256','bytes32'],[scope,owner,nonce,bytes.fromhex(args[0][2:])])).hex()
    else:new=old;args=['0x'+old]
    method='createRefund' if mode=='CREATE_BTC_REFUND' else 'refundTokens'
    operation=dict(method=method,args=args,request_id=new,principal_request=old,principal_sats='5000',expected_nonce='0' if method=='createRefund' else None)
    view=NS(closed=False,created=False,nonce=0)
    values=dict(getBudget=lambda _:[owner,address(2),address(0),1 if method=='createRefund' else 2 if mode=='REFUND_MP' else 3,
        1,0,100,100,0,1000,bytes(32),bytes(32),bytes.fromhex(script),view.closed],
        refundNonce=lambda _:view.nonce,scope=scope,refundRequest=lambda _:view.created,refundOrigin=lambda _:bytes.fromhex(old))
    class Methods(Functions):
        def __getattr__(self,name):
            if name in ('refundTokens','createRefund'):return lambda *args:NS(call=lambda *a,**kw:None)
            return super().__getattr__(name)
    credits=NS(address=address(70),functions=Methods(values),events=NS(Consented=lambda:NS(process_receipt=lambda r,**kw:[dict(args=dict(request=bytes.fromhex(new)))])))
    ctx=dict(block=NS(number=200),common=NS(address=address(71)),escrow=NS(functions=Functions(dict(getRequest=lambda _:
        [address(0),0,address(2),owner,5000*10**10,b'',bytes(32),1]))),withdrawal_fees=dict(scope=scope.hex(),credits=credits,funding=NS(address=address(72))))
    i.context=lambda:ctx;i.w3=e.w3;i.fence=lambda ctx:None
    r=fee.Runtime.__new__(fee.Runtime);r.c=dict(mode=mode,version='1');r.wc={};r.request=operation;r.i=i;r.actors=dict(operator=i)
    r.journal=i.journal;r.job='refund-fixture';r.prepare_only=False;r.sdk=Owner(i);r.context=lambda:ctx
    def effect(tx,receipt):
        if method=='createRefund':view.created=True;view.nonce+=1
        else:view.closed=True
    e.effect=effect
    cap='withdrawal_btc' if method=='createRefund' else 'withdrawal_mp' if mode=='REFUND_MP' else 'withdrawal_s'
    def call(action='run'):
        p=fee.Pipeline(r,r.c,dict(capability=cap));return p,p.execute(action)
    return NS(e=e,r=r,call=call,view=view,ctx=ctx,mode=mode)


def test_original_owner_refund_uses_exact_accepted_args_and_one_shared_nonce(refund):
    f=refund;p,result=f.call()
    assert result['status']=='SATISFIED'
    assert len(f.e.raws)==1 and len(result['observations']['transactions'])==1
    tx=result['observations']['transactions'][0]
    assert tx['profile']['steps'][0]['method']==f.r.request['method']
    assert tx['original_transaction']['result']['raw_transaction']==f.e.raws[0].hex()
    assert f.call('probe')[1]['status']=='SATISFIED' and len(f.e.raws)==1
    # Lost local completion adopts the same original envelope, including the
    # pre-increment createRefund id. No new refundNonce is used.
    p.record['calls']={};p.save()
    assert f.call('recover')[1]['status']=='SATISFIED' and len(f.e.raws)==1


def test_probe_and_unenrolled_recovery_cannot_create_refunds(refund):
    f=refund
    assert f.call('probe')[1]['status']=='ABSENT'
    assert f.call('recover')[1]['status']=='RECOVERY_REQUIRED'
    assert not f.e.raws


def test_unsigned_crash_is_not_retried_with_another_nonce(refund):
    f=refund;f.e.crash='unsigned'
    with pytest.raises(RuntimeError):f.call()
    f.e.crash=None
    assert f.call('recover')[1]['status']=='RECOVERY_REQUIRED' and not f.e.raws


def test_refund_nonce_is_checked_before_signature_and_not_after_receipt(refund):
    f=refund
    if f.mode!='CREATE_BTC_REFUND':return
    f.view.nonce=1
    with pytest.raises(ValueError,match='REFUND_NONCE'):f.call()
    assert not f.e.raws
    f.view.nonce=0
    assert f.call('recover')[1]['status']=='RECOVERY_REQUIRED'  # Retained unsigned intent.


def test_missing_canonical_receipt_holds_without_new_refund_or_token_return(refund):
    f=refund;f.call();f.e.receipts.clear()
    assert f.call('recover')[1]['status']=='RECOVERY_REQUIRED' and len(f.e.raws)==1


def test_personal_refund_cannot_relabel_a_different_principal_request(tmp_path,monkeypatch):
    wc=config(tmp_path);fc=dict(bitcoin_network='mainnet',**{k:wc[k] for k in ('chain_id','bitcoin_policy','evm_confirmations')})
    monkeypatch.setattr(fee,'fee_config',lambda path:fc)
    body=dict(method='refundTokens',args=['0x'+'11'*32],request_id='11'*32,principal_request='22'*32,principal_sats='5000',expected_nonce=None)
    path=tmp_path/'request.json';write(path,body);c=dict(mode='REFUND_MP',fee_config='/fee.json',request_file=str(path),accepted_request_sha256=digest(body))
    with pytest.raises(ValueError,match='CONTOUR_FEE_REQUEST'):fee.inputs(c,wc)


def test_exact_confirmed_original_call_can_be_adopted_into_new_evidence_without_spending(envelopes):
    e=envelopes;e.controller();approve(e);original=deepcopy(e.record)
    e.record={'calls':{}};c=e.controller('probe');approve(e)
    assert e.record==original and len(e.raws)==1 and len(c.evidence())==1


@pytest.fixture
def ingress(envelopes,funding_env,monkeypatch):
    from signer_agent import withdrawal_fee_funding as sdk
    from signer_agent.bootstrap import artifact
    from web3 import Web3
    e=envelopes;bitcoin=funding_env;bitcoin.transaction(coinbase=True);bitcoin.view.confirmations=101
    view=NS(classified=0,claim=[bytes(32),bytes(32),0,bytes(32)],fresh=False,bad_merkle=False)
    i=e.actors['operator'];scope='aa'*32;script=bitcoin.b['script'].hex();intent=NS(request=b'r'*32)
    credits=NS(address=address(70),functions=Functions(dict(getClaim=lambda _:view.claim)))
    node=NS(address=address(72),functions=Functions(dict(classified=lambda _:view.classified)))
    common=NS(address=address(71),functions=Functions(dict(depth=6,feeScripts=lambda _:b'i'*32,custodyTransactions=lambda _:False,spv=address(73))))
    spv=NS(functions=Functions(dict(isFresh=lambda:view.fresh,getBlockStatus=lambda _:(True,101),checkTxInclusion=lambda *a:True)))
    ctx=dict(block=NS(number=200),pool=NS(address=address(74)),common=common,
        registry=NS(functions=Functions(dict(custodyGeneration=1,derivationGeneration=2))),withdrawal_fees=dict(scope=scope,credits=credits,funding=node))
    old_rpc=bitcoin.bitcoin.call
    def rpc(method,*args):
        if method=='getbestblockhash':return 'bb'*32
        if method=='getblock' and view.bad_merkle:return dict(tx=[bitcoin.view.txid],merkleroot='00'*32,confirmations=101)
        return old_rpc(method,*args)
    i.bitcoin=NS(call=rpc,check_network=lambda:None);i.context=lambda:ctx;i.w3=e.w3;i.fence=lambda ctx:None
    e.w3.eth.contract=lambda **kw:spv
    monkeypatch.setattr(sdk,'funding_binding',lambda *a:(intent,dict(script_pubkey=script),b'p'*32))
    r=fee.Runtime.__new__(fee.Runtime);r.c=dict(mode='FUNDING',version='1');r.request=dict(kind='withdrawal_fee',consumer=ctx['pool'].address,intent_id=(b'i'*32).hex(),txid=bitcoin.view.txid,vout='0')
    r.i=i;r.actors=dict(operator=i);r.journal=i.journal;r.prepare_only=True;r.job='ingress';r.context=lambda:ctx
    r.sdk=Funding(i);r.sdk.fence=lambda ctx:None
    contract=Web3().eth.contract(address=node.address,abi=artifact('WithdrawalFeeFunding')['abi'])
    def effect(tx,receipt):
        fn,args=contract.decode_function_input(bytes(tx['data']))
        if fn.fn_name=='classify':view.classified=1
        if fn.fn_name=='admit':view.claim=[intent.request,b'l'*32,1000,bytes(32)]
    e.effect=effect
    def call(action='run'):
        p=fee.Pipeline(r,r.c,dict(capability='bitcoin_funding'));return p,p.execute(action)
    return NS(e=e,r=r,view=view,call=call)


def test_original_fee_ingress_classifies_then_waits_for_proof_and_admits_exact_credit(ingress):
    f=ingress
    assert f.call()[1]['status']=='SATISFIED' and len(f.e.raws)==1
    assert f.call('recover')[1]['status']=='SATISFIED'  # Preparation only, no credit claimed.
    assert bytes(f.view.claim[0])==bytes(32)
    f.r.prepare_only=False;f.r.job='admit'
    assert f.call()[1]['status']=='WAITING' and len(f.e.raws)==1
    f.view.fresh=True
    assert f.call('recover')[1]['status']=='SATISFIED' and len(f.e.raws)==2
    assert f.view.claim[0]==b'r'*32
    assert f.call('probe')[1]['status']=='SATISFIED' and len(f.e.raws)==2


def test_bad_native_merkle_proof_cannot_spend_fee_admission_budget(ingress):
    f=ingress;f.call();f.view.bad_merkle=True;f.view.fresh=True
    with pytest.raises(ValueError,match='MERKLE_ROOT_MISMATCH'):f.call('recover')
    assert len(f.e.raws)==1 and f.view.claim[0]==bytes(32)

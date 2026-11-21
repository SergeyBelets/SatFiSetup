"""Original ABI/EOA/SQLite, budgets and native Bitcoin codec; Core/FROST/EVM effects are fixtures."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from eth_account import Account
from eth_account._utils.legacy_transactions import Transaction
from web3 import Web3
from web3.datastructures import AttributeDict
from web3.exceptions import TransactionNotFound

from ops.zkspv import contour_calls as calls,contour_withdrawal as w,contour_reconcile as rec,contour_plan
from ops.zkspv.contour import digest
from signer_agent.bootstrap import Bootstrap,artifact
from signer_agent.operations import Journal
from signer_agent.btc_fee_client import Driver
from signer_agent.bitcoin_worker import BitcoinWorker
from signer_agent import bitcoin_onchain,crypto,btc_fee_policy
from signer_agent.secure_state import SecureState
from signer_agent.zk_worker_prover import write
from test_address_mint import address
from test_address_watch import tx
from test_btc_fee_reserve import fee_env,fee_driver,report
from test_bitcoin_input_keys import signing,native_keys
from test_withdrawal_fee import personal
from test_zk_worker import config

ORIGINAL_SEND=Driver.send
class Receipt(dict):
    def __getattr__(self,key):return self[key]


@pytest.fixture
def envelopes(tmp_path,monkeypatch):
    tmp_path.chmod(0o700)
    e=NS(actors={},transactions={},receipts={},raws=[],methods=[],crash=None,guard=True,record={'calls':{}},saved=[])
    for n,role in enumerate(('owner','operator'),1):
        identity=tmp_path/(role+'-identity.json');write(identity,dict(private_key=n.to_bytes(32,'big').hex()))
        c=dict(identity_file=str(identity),reserve=address(40),chain_id='31337',evm_rpc='http://127.0.0.1:1',
            evm_confirmations='1',bitcoin_network='regtest',bitcoin_policy=None,max_tx_gas='1000000',
            max_transaction_cost_wei='1000000',max_total_cost_wei='20000000',max_job_cost_wei='10000000',max_attempts_per_job='32')
        e.actors[role]=NS(c=c,journal=Journal(tmp_path/(role+'.sqlite')))
    e.job='fixture';e.allowed=lambda role,profile:None
    def block(n):return AttributeDict(dict(number=n if isinstance(n,int) else 200,hash=b'h'*32,timestamp=100))
    def receipt(txid):
        if txid not in e.receipts:raise TransactionNotFound(txid)
        return e.receipts[txid]
    def transaction(txid):
        if txid not in e.transactions:raise TransactionNotFound(txid)
        return e.transactions[txid]
    def send(raw):
        txid=Web3.keccak(raw).hex()
        if txid in e.transactions:return bytes.fromhex(txid)
        txdata=Transaction.from_bytes(raw).as_dict();owner=Account.recover_transaction(raw)
        assert txdata['nonce']==sum(Account.recover_transaction(r)==owner for r in e.raws)
        e.transactions[txid]=txdata;e.raws.append(raw)
        e.receipts[txid]=Receipt(status=1,blockNumber=10,blockHash=b'h'*32,transactionHash=bytes.fromhex(txid),logs=[])
        if hasattr(e,'effect'):e.effect(txdata,e.receipts[txid])
        return bytes.fromhex(txid)
    e.w3=NS(provider=NS(endpoint_uri='http://127.0.0.1:1'),eth=NS(chain_id=31337,block_number=200,get_block=block,
        get_code=lambda *a,**kw:b'fixture',get_transaction=transaction,get_transaction_receipt=receipt,
        get_transaction_count=lambda owner,*a:sum(Account.recover_transaction(r)==owner for r in e.raws),
        send_raw_transaction=send,wait_for_transaction_receipt=lambda txid,**kw:receipt(txid)))
    e.registry=NS(w3=e.w3,identity_file=e.actors['operator'].c['identity_file'],bitcoin_graph=dict(version='1',bitcoin_network='regtest'))
    e.registry.transaction_journal=e.registry.identity_file+'.transactions.sqlite'
    def runner(controller,profile,actor,budget=None,budget_key=None):
        b=Bootstrap.__new__(Bootstrap);b.profile=profile;b.prefix=profile['deployment_id'];b.w3=e.w3
        b.journal=Journal(actor.c['identity_file']+'.transactions.sqlite');b.journal.put(b.prefix,dict(profile=profile,version='1'))
        b.contracts=profile['contracts'];b.values={};b.check_external_components=lambda:None
        b.contract=lambda name:Web3().eth.contract(address=profile['contracts'][name]['address'],abi=artifact(profile['contracts'][name]['artifact'])['abi'])
        b.manifest=lambda:dict(deployment_id=b.prefix)
        def build(step,account,nonce):
            if e.crash=='unsigned':raise RuntimeError('unsigned fixture interruption')
            txdata=dict(chainId=31337,nonce=nonce,to=profile['contracts'][step['contract']]['address'],gas=200000,gasPrice=1,value=0,
                data=b.fn(step)._encode_transaction_data())
            assert budget is not None
            budget.reserve(budget_key,txdata);return txdata
        b.build_transaction=build;return b
    monkeypatch.setattr(calls.Calls,'runner',runner)
    def guard():
        if not e.guard:raise ValueError('fixture generation changed')
    e.guard_fn=guard
    e.driver=lambda role:Driver(e.actors[role],dict(onchain_board=address(50)))
    def controller(action='run'):
        c=calls.Calls(e,e.record,lambda:e.saved.append(deepcopy(e.record)),action)
        for actor in e.actors.values():actor.transaction_controller=c
        e.registry.transaction_controller=c;return c
    e.controller=controller
    return e


def approve(e,role='owner',key='accepted-primary',amount=10):
    return e.driver(role).send(key,'PoolMpToken',address(60),'approve',[address(61),amount],e.guard_fn)


def test_fee_sdk_uses_original_budget_guard_and_separate_actor_nonce_lanes(envelopes):
    e=envelopes
    for role in e.actors:e.controller();approve(e,role)
    assert len(e.raws)==2 and [Transaction.from_bytes(r).nonce for r in e.raws]==[0,0]
    assert len({Account.recover_transaction(r) for r in e.raws})==2
    for actor in e.actors.values():
        budget=Journal(actor.c['identity_file']+'.spv-service-budget.sqlite')
        with budget.db() as db:assert db.execute('SELECT count(*) FROM spv_costs').fetchone()[0]==1
    e.guard=False;e.controller();approve(e)
    assert len(e.raws)==2


def test_calls_probe_cannot_enroll_sign_or_consume_budget(envelopes):
    e=envelopes;e.controller('probe')
    with pytest.raises(calls.Required):approve(e)
    assert not e.raws and not e.record['calls'] and not e.saved


def test_one_original_operation_per_action(envelopes):
    e=envelopes;e.controller();approve(e)
    with pytest.raises(calls.Progress):approve(e,'operator')
    assert len(e.raws)==1


@pytest.mark.parametrize('failure',['unsigned','guard','budget'])
def test_pre_sign_interruption_holds_without_new_nonce(envelopes,failure):
    e=envelopes
    if failure=='unsigned':e.crash='unsigned'
    if failure=='guard':e.guard=False
    if failure=='budget':e.actors['owner'].c['max_transaction_cost_wei']='1'
    e.controller()
    with pytest.raises((RuntimeError,ValueError)):approve(e)
    e.crash=None;e.guard=True
    with pytest.raises(calls.Hold):e.controller('recover').pending()
    assert not e.raws and len(e.record['calls'])==1


def test_signed_global_nonce_recovery_precedes_new_generation_guard(envelopes,monkeypatch):
    e=envelopes;original=Journal.update
    def update(j,key,status,result):
        if str(j.path)==e.actors['owner'].c['identity_file']+'.transactions.sqlite' and status=='SIGNED':raise RuntimeError('signed fixture interruption')
        return original(j,key,status,result)
    monkeypatch.setattr(Journal,'update',update);e.controller()
    with pytest.raises(RuntimeError,match='signed'):approve(e)
    assert not e.raws
    monkeypatch.setattr(Journal,'update',original);e.guard=False
    with pytest.raises(calls.Progress):e.controller('recover').pending()
    assert len(e.raws)==1 and Transaction.from_bytes(e.raws[0]).nonce==0
    e.controller('probe').pending()


@pytest.mark.parametrize('mutation',['receipt','reorg','raw'])
def test_exact_receipt_and_original_envelope_required(envelopes,mutation):
    e=envelopes;e.controller();approve(e);saved=next(iter(e.record['calls'].values()));actor=e.actors['owner']
    if mutation=='receipt':e.receipts.clear()
    if mutation=='reorg':next(iter(e.receipts.values()))['blockHash']=b'z'*32
    if mutation=='raw':
        j=Journal(actor.c['identity_file']+'.transactions.sqlite');key=saved['profile']['deployment_id']+':send';row=j.get(key)
        row['result']['nonce']='7';j.update(key,'CONFIRMED',row['result'])
    error=calls.ObservationPending if mutation=='receipt' else ValueError
    with pytest.raises(error):e.controller('probe').pending()
    assert len(e.raws)==1


def test_original_bitcoin_sdk_shares_operator_nonce_lane_and_has_bounded_budget(envelopes):
    e=envelopes;e.controller();approve(e,'operator')
    e.controller();bitcoin_onchain.send(e.registry,address(62),'BitcoinSigningBoard','publishPlan',[b'b'*32,b'public plan'])
    assert [Transaction.from_bytes(r).nonce for r in e.raws]==[0,1]
    e.controller('probe').pending();assert len(e.record['calls'])==2


@pytest.fixture(params=['BTC','MP','S'])
def withdrawal(envelopes,request,tmp_path,monkeypatch):
    e=envelopes;e.mode=request.param;e.quote=dict(cost=dict(principal_sats='5000'));e.plan=dict(batch_id='bb'*32)
    raw=tx(bytes.fromhex('5120'+'11'*32),5000,previous='22'*32+':0',tag=1).hex()
    from signer_agent.bitcoin_proofs import sha256d
    parsed=crypto.call('bitcoin_funding',raw=raw,txid=sha256d(bytes.fromhex(raw))[::-1].hex())
    e.need=dict(kind='withdrawal',consumer=address(63),batch=e.plan['batch_id'],txid=parsed['txid'])
    e.c=dict(version='1',mode=e.mode,state_file=str(tmp_path/'participant.enc'))
    e.sent_btc=[];e.btc_lost=False;e.btc_known=False;e.frost=False;e.btc_ready=False;e.paid=False
    class Core:
        def check_network(self):pass
        def call(self,method,*args):
            if method=='getrawtransaction':
                if not e.btc_known:raise ValueError('fixture unbroadcast')
                return raw
            if method=='testmempoolaccept':return [dict(allowed=True)]
            if method=='sendrawtransaction':
                e.sent_btc.append(args[0]);e.btc_known=True
                if e.btc_lost:raise ConnectionError('fixture lost Bitcoin response')
                return e.need['txid']
            raise AssertionError(method)
    e.actors['operator'].bitcoin=Core()
    # Exercise the original native codec and original durable Bitcoin broadcast/recovery.
    e.broadcast_record=lambda:w.Runtime.broadcast_record(e)
    e.recover_broadcast=lambda:w.Runtime.recover_broadcast(e)
    monkeypatch.setattr('signer_agent.bitcoin_input_keys.build',lambda plan:dict(unsigned=parsed['stripped']))
    def prepare():
        approve(e)
        if not e.frost:return dict(status='WAITING_EXTERNAL_FROST')
        bitcoin_onchain.send(e.registry,address(62),'BitcoinSigningBoard','publishPlan',[b'b'*32,b'public plan'])
        if not e.btc_ready:return dict(status='AWAITING_COMMITMENTS')
        e.before_broadcast()
        BitcoinWorker(e.c['state_file']+'.bitcoin.sqlite',e.actors['operator'].bitcoin).broadcast(e.plan['batch_id'],
            dict(signed=raw,txid=e.need['txid'],wtxid=parsed['wtxid']))
        return dict(status='BTC_BROADCAST')
    e.prepare=prepare
    e.settled=lambda:dict(primary_state='PAID',request='rr',cost=e.quote['cost']) if e.paid else None
    def btc(action):
        assert action=='btc-settle'
        bitcoin_onchain.send(e.registry,e.need['consumer'],'BitcoinWithdrawalEscrow','settle',[b'b'*32,bytes.fromhex(raw),b'h'*32,[],0])
        e.paid=True;return dict(status='PAID')
    e.btc=btc
    def execute(action='probe',phase='withdrawal_prepare'):
        return w.Pipeline(e,e.c,e.need,dict(capability=phase)).execute(action)
    e.execute=execute
    return e


def prepared(e):
    assert e.execute('run')['status']=='WAITING'
    e.frost=True
    assert e.execute('recover')['status']=='WAITING'
    e.btc_ready=True
    assert e.execute('recover')['status']=='WAITING'
    assert e.execute()['status']=='SATISFIED'


def test_selected_withdrawal_waits_external_frost_broadcasts_and_settles_exactly_once(withdrawal):
    e=withdrawal;prepared(e);phase='withdrawal_'+e.mode.lower()
    assert len(e.sent_btc)==1 and len(e.raws)==2
    assert e.execute('run',phase)['status']=='ABSENT'
    result=e.execute('probe',phase)
    assert result['status']=='SATISFIED' and result['observations']['principal_sats']=='5000' and not result['observations']['ready']
    assert e.execute('recover',phase)['status']=='SATISFIED' and len(e.raws)==3 and len(e.sent_btc)==1


def test_waiting_for_external_participants_never_creates_private_nonces(withdrawal):
    e=withdrawal;e.execute('run')
    for _ in range(3):assert e.execute()['status']=='WAITING'
    assert len(e.raws)==1 and not e.sent_btc and not Path(e.c['state_file']).exists()


def test_lost_bitcoin_response_recovers_original_raw_before_new_prepare_guards(withdrawal):
    e=withdrawal;e.execute('run');e.frost=True;e.execute('recover');e.btc_ready=True;e.btc_lost=True
    with pytest.raises(ConnectionError):e.execute('recover')
    e.prepare=lambda:(_ for _ in ()).throw(AssertionError('new prepare must not run'))
    e.btc_lost=False
    assert e.execute('recover')['status']=='ABSENT'
    assert e.execute()['status']=='SATISFIED' and len(e.sent_btc)==1 and len(e.raws)==2


def test_broadcast_latch_without_signed_bytes_is_an_explicit_hold(withdrawal):
    e=withdrawal;e.execute('run');p=w.Pipeline(e,e.c,e.need,dict(capability='withdrawal_prepare'))
    row=p.journal.get(p.key)['result'];row['broadcast_started']=True;p.journal.update(p.key,'ENROLLED',row)
    assert e.execute()['status']=='RECOVERY_REQUIRED' and not e.sent_btc


def test_wrong_phase_or_unenrolled_recovery_never_sends(withdrawal):
    e=withdrawal
    with pytest.raises(ValueError,match='CAPABILITY'):e.execute('run','address_mint')
    assert e.execute('recover')['status']=='RECOVERY_REQUIRED' and not e.raws


def test_external_completion_without_original_settlement_receipt_is_a_hold(withdrawal):
    e=withdrawal;e.paid=True
    assert e.execute('run','withdrawal_'+e.mode.lower())['status']=='RECOVERY_REQUIRED' and not e.raws


@pytest.mark.parametrize('stage',range(6))
def test_original_preparation_order_and_external_authorization_waits(stage):
    r=w.Runtime.__new__(w.Runtime);r.events=[];r.plan=dict(batch_id='bb'*32)
    def mark(name,result=None):r.events.append(name);return result
    r.primary=lambda:mark('primary');r.payment=lambda:mark('payment',stage>0);r.version=lambda:mark('version')
    r.graph_check=lambda:(dict(batch=NS(functions=NS(batchState=lambda *a:NS(call=lambda **kw:0 if stage<3 else 1 if stage<5 else 2))),block=NS(number=10)),None)
    r.session_ready=lambda operation:None if stage in (1,3) else False if stage==2 else True
    r.btc=lambda action,operation=None:mark((action,operation),dict(status='AWAITING_COMMITMENTS'))
    result=r.prepare()
    assert r.events[:2]==['primary','payment']
    if stage==0:assert result['status']=='WAITING_FEE_AUTHORIZATION'
    else:
        assert r.events[2:4]==['version',('btc-import-plan',None)]
        assert all(event not in ('btc-commit','btc-sign','btc-retry') for event in r.events)
        if stage<3:assert result['status']=='WAITING_RESERVE_AUTHORIZATION'
        elif stage==3:assert result['status']=='WAITING_BIND_AUTHORIZATION'
        elif stage==4:assert result['status']=='WITHDRAWAL_PREPARATION_PROGRESS'
        else:assert result['status']=='AWAITING_COMMITMENTS'


@pytest.fixture
def reconciliation(fee_driver,envelopes,tmp_path,monkeypatch):
    f=fee_driver;e=envelopes;f.a=f.driver.authority(f.ctx)
    # Keep original Inspector reconciliation, typed policies and Driver requests;
    # only public RPC state transitions/graph origins are fixtures.
    old_eth=f.i.w3.eth;f.i.w3=e.w3;e.w3.eth.contract=old_eth.contract
    e.w3.eth.get_block=lambda n:f.block
    f.i.c.update(e.actors['operator'].c);e.actors['operator']=f.i
    monkeypatch.setattr(Driver,'send',ORIGINAL_SEND)
    r=rec.Runtime.__new__(rec.Runtime);r.i=f.i;r.driver=f.driver;r.bundle=f.bundle;r.actors=dict(operator=f.i);r.job='fixture'
    r.c=dict(version='1',report_id='11'*32,deadline='400');r.state=SecureState(str(tmp_path/'reconcile.enc'),'fixture')
    r.allowed=lambda *a:None
    def effect(txdata,receipt):
        target=Web3.to_checksum_address(bytes(txdata['to']).hex())
        kind={f.a['coordinator'].address:'FROSTCoordinator',f.a['board'].address:'DKGOnchainBoard',
            f.a['authorization'].address:'DKGAuthorization',f.fee.address:'BitcoinFeeReserve'}[target]
        fn,args=Web3().eth.contract(address=target,abi=artifact(kind)['abi']).decode_function_input(txdata['data'])
        values=list(args.values());method=fn.fn_name;e.methods.append(method)
        if method=='createSessionFromDKG':
            sid=len(f.sessions)+1;f.sessions[sid]=[sid,address(23),b'g'*32,values[1],True,2,2,values[3],True,address(0),1,0,0,0,0,f.fee.address]
            receipt['events']=[dict(args=dict(sessionId=sid))]
        elif method=='publishIntent':f.public[int(values[0])]=values[1]
        elif method=='authorizeOperation':f.approved.add(values[1])
        elif method=='publishReconciliation':
            p=rec.Pipeline(r,r.c,dict(capability='reconciliation'))
            data=btc_fee_policy.action(method,p.journal.get(p.key)['result']['request']['metadata']['args'])[2]
            f.receipts[values[-1]]=Web3.keccak(data);f.sequence+=1
        else:raise AssertionError(method)
    e.effect=effect
    f.r=r;f.envelopes=e;f.execute=lambda action='probe':rec.Pipeline(r,r.c,dict(capability='reconciliation')).execute(action)
    return f


@pytest.mark.parametrize('failure',[None,'missing','pruned'])
def test_full_inventory_report_waits_frost_and_never_clears_incident_pause(reconciliation,failure):
    f=reconciliation
    if failure=='missing':f.view.missing.add(f.ids[1])
    if failure=='pruned':f.view.pruned=True
    assert f.execute()['status']=='ABSENT'
    assert f.execute('run')['status']=='ABSENT'
    for _ in range(3):f.execute('recover')
    assert f.execute()['status']=='WAITING' and len(f.sessions)==1
    f.sessions[1][10]=2
    for _ in range(5):
        result=f.execute('recover')
        if result['status'] in ('SATISFIED','RECOVERY_REQUIRED'):break
    assert result['status']==('SATISFIED' if failure is None else 'RECOVERY_REQUIRED')
    assert f.envelopes.methods==['createSessionFromDKG','publishIntent','authorizeOperation','publishReconciliation']
    if failure=='missing':
        marker=f.r.state.get('btc-payments-recovery:'+b'context'.ljust(32,b'\0').hex());assert marker
        f.view.missing.clear();f.execute('recover')
        assert f.r.state.get('btc-payments-recovery:'+b'context'.ljust(32,b'\0').hex())==marker
    assert not result['observations']['ready'] and f.view.backing==500


def test_reconciliation_uses_same_snapshot_after_interruption_and_no_auto_renewal(reconciliation,monkeypatch):
    f=reconciliation;original=f.r.prepare
    f.r.prepare=lambda observed:(_ for _ in ()).throw(RuntimeError('fixture after observation'))
    with pytest.raises(RuntimeError):f.execute('run')
    p=rec.Pipeline(f.r,f.r.c,dict(capability='reconciliation'));saved=p.journal.get(p.key)['result']['observation']
    f.r.observe=lambda:(_ for _ in ()).throw(AssertionError('must retain original scan'))
    f.r.prepare=lambda observed:original(observed) if observed==saved else (_ for _ in ()).throw(AssertionError())
    assert f.execute('recover')['status']=='ABSENT'
    count=len(f.envelopes.raws);f.view.cg+=1
    with pytest.raises(ValueError):f.execute('recover')
    assert len(f.envelopes.raws)==count and len(f.sessions)==1


def test_reconciliation_unenrolled_recovery_does_not_scan(reconciliation):
    f=reconciliation;f.r.observe=lambda:(_ for _ in ()).throw(AssertionError())
    assert f.execute('recover')['status']=='RECOVERY_REQUIRED' and not f.envelopes.raws


def test_candidate_terms_hash_is_exactly_shared_by_offline_and_onchain_quote():
    from signer_agent.withdrawal_fee_client import candidate_terms
    from eth_abi import encode
    from eth_utils.abi import collapse_if_tuple
    candidate=[[0,1,2,400],b'b'*32,bytes(32),b'p'*32,b'raw',[b'r'*32],[[b'r'*32,1,100]],[],bytes(32)]
    spec=next(x for x in artifact('WithdrawalFeePayments')['abi'] if x.get('name')=='getPrepareOperationHash')['inputs'][0]
    expected=Web3.keccak(encode(['bytes32',collapse_if_tuple(spec)],[b's'*32,candidate]))
    assert candidate_terms(b's'*32,candidate)==expected
    candidate[6][0][2]+=1
    assert candidate_terms(b's'*32,candidate)!=expected


@pytest.fixture
def selected(personal,tmp_path):
    from dataclasses import replace
    from signer_agent.bitcoin_input_keys import build
    e,_,_=personal;plan=deepcopy(e.plan);plan['network']='mainnet';wc=config(tmp_path)
    wc['allowed_consumers']={e.escrow.address:'BitcoinWithdrawalEscrow'}
    fc=json.loads(Path('config/withdrawal-fee.example.json').read_text())
    fc.update(bitcoin_network='mainnet',bitcoin_policy=wc['bitcoin_policy'])
    quote=dict(options=dict(fee_mode='MP',asset='MP',deadline='200'),graph=dict(reserve=fc['reserve'],escrow=e.escrow.address),
        chain_id='31337',bitcoin_network='mainnet')
    payment=dict(plan=plan,terms_hash='ee'*32,request=dict(metadata=dict(method='prepare',bitcoin_plan=plan)))
    bundle=e.manifest.to_dict();auth=replace(e.manifest,purpose='EVM_AUTH',session_id=b'e'*32).to_dict();auth['role']='conf_ceremony'
    c=json.loads(Path('config/zkspv-contour-withdrawal.example.json').read_text())
    for key,name in [('owner_config','owner.json'),('operator_config','operator.json'),('quote_file','quote.json'),
                     ('payment_file','payment.json'),('bundle_file','btc.json'),('auth_bundle_file','auth.json')]:c[key]=str(tmp_path/name)
    c.update(accepted_quote_hash=digest(quote),accepted_terms_hash=payment['terms_hash'],request_id=plan['withdrawal_refs'][0])
    need=dict(kind='withdrawal',consumer=e.escrow.address,batch=plan['batch_id'],txid=build(plan)['txid'])
    for name,value in [('owner.json',fc),('operator.json',fc),('quote.json',quote),('payment.json',payment),('btc.json',bundle),
                       ('auth.json',auth),('withdrawal.json',c),('worker.json',wc),('need.json',need)]:write(tmp_path/name,value)
    rc=json.loads(Path('config/zkspv-contour-reconciliation.example.json').read_text());rc.update(fee_config=c['operator_config'],bundle_file=c['auth_bundle_file'])
    write(tmp_path/'reconcile.json',rc)
    return NS(**locals())


@pytest.mark.parametrize('field,value',[('mode','AUTO'),('owner_config','relative'),('accepted_terms_hash','AB'*32),
    ('auth_bundle_file',None),('version',1)])
def test_withdrawal_config_requires_explicit_reviewed_inputs(selected,field,value):
    e=selected;c=dict(e.c,**{field:value});write(e.tmp_path/'withdrawal.json',c)
    with pytest.raises(ValueError,match='CONFIG'):w.load_config(e.tmp_path/'withdrawal.json')


@pytest.mark.parametrize('change',['quote','mode','terms','request','previous','need','chain','authority'])
def test_offline_withdrawal_scope_checks_reject_substitution(selected,change):
    e=selected;c=deepcopy(e.c);need=deepcopy(e.need);wc=deepcopy(e.wc)
    if change=='quote':c['accepted_quote_hash']='aa'*32
    if change=='mode':c['mode']='S'
    if change=='terms':c['accepted_terms_hash']='aa'*32
    if change=='request':c['request_id']='aa'*32
    if change=='previous':
        e.payment['plan']['withdrawal_fee']['previous']='01'*32;write(e.tmp_path/'payment.json',e.payment)
    if change=='need':need['txid']='aa'*32
    if change=='chain':wc['chain_id']='1'
    if change=='authority':
        e.auth['purpose']='BTC_SPEND';write(e.tmp_path/'auth.json',e.auth)
    with pytest.raises(ValueError):w.inputs(c,wc,need)


@pytest.mark.parametrize('mode',['BTC','MP','S'])
def test_offline_builder_connects_one_selected_version_and_reconciliation(selected,mode):
    e=selected;e.quote['options'].update(fee_mode=mode,asset='S' if mode=='S' else 'MP')
    e.c.update(mode=mode,accepted_quote_hash=digest(e.quote))
    write(e.tmp_path/'quote.json',e.quote);write(e.tmp_path/'withdrawal.json',e.c)
    plan=contour_plan.build(e.tmp_path/'worker.json',e.tmp_path/'need.json',e.tmp_path/'run',
        withdrawal_config=e.tmp_path/'withdrawal.json',reconciliation_config=e.tmp_path/'reconcile.json',worker_only=True)
    caps=[s['capability'] for s in plan['stages']]
    assert caps==['withdrawal_prepare','consumer_request','proof_preparation','fresh_proof','state_update','membership','withdrawal_'+mode.lower(),'reconciliation']
    args=plan['stages'][0]['run'];data=w.inputs(e.c,e.wc,e.need)
    assert digest([data[0],data[1],data[2],data[4],data[5],data[6]]) in args
    assert all(s['probe']==s['run']==s['recover'] for s in plan['stages'])
    partial=contour_plan.build(e.tmp_path/'worker.json',e.tmp_path/'need.json',e.tmp_path/'partial',
        withdrawal_config=e.tmp_path/'withdrawal.json',reconciliation_config=e.tmp_path/'reconcile.json',settlement_only=True)
    assert [s['capability'] for s in partial['stages']]==['membership','withdrawal_'+mode.lower(),'reconciliation']


@pytest.mark.parametrize('kwargs',[dict(mint_config='unused'),dict(funding_config='unused'),dict(bootstrap_config='unused'),dict(consumer_only=True)])
def test_builder_rejects_mixed_mint_funding_and_withdrawal(selected,kwargs):
    e=selected
    with pytest.raises(ValueError,match='WITHDRAWAL_CONFIG'):
        contour_plan.build(e.tmp_path/'worker.json',e.tmp_path/'need.json',e.tmp_path/'run',
            withdrawal_config=e.tmp_path/'withdrawal.json',**kwargs)


@pytest.mark.parametrize('field,value',[('deadline','0400'),('report_id','AB'*32),('state_file','relative'),('version',1)])
def test_reconciliation_config_is_explicit_canonical_and_private(selected,field,value):
    e=selected;write(e.tmp_path/'reconcile.json',dict(e.rc,**{field:value}))
    with pytest.raises(ValueError):rec.load_config(e.tmp_path/'reconcile.json')


def test_reconciliation_requires_original_evm_auth_bundle(selected):
    e=selected
    assert rec.inputs(e.rc,e.wc)==(e.fc,e.auth)
    e.rc['bundle_file']=e.c['bundle_file']
    with pytest.raises(ValueError,match='SCOPE'):rec.inputs(e.rc,e.wc)


def test_bitcoin_dispatch_imports_frozen_plan_without_duplicate_payment_envelope(selected,monkeypatch):
    e=selected;r=w.Runtime.__new__(w.Runtime);r.c=dict(e.c,state_file=str(e.tmp_path/'participant.enc'))
    r.bundle=dict(e.bundle,onchain_board=address(99));r.manifest=e.e.manifest;r.plan=e.plan;r.wc=e.wc
    r.key={};r.registry=object();r.state=object();seen=[]
    monkeypatch.setattr(bitcoin_onchain,'dispatch',lambda args,*a:seen.append(args))
    r.btc('btc-import-plan');r.btc('btc-import-plan')
    path=Path(seen[0].plan_file)
    assert json.loads(path.read_text())==e.plan and path.stat().st_mode&0o077==0
    assert path!=Path(e.c['payment_file']) and 'request' not in json.loads(path.read_text())


@pytest.mark.parametrize('role,kind,method,valid',[
    ('owner','WithdrawalFeeCredits','approveVersion',True),('operator','WithdrawalFeePayments','prepare',True),
    ('operator','BitcoinSigningBoard','appendFeePlan',True),('operator','DKGOnchainBoard','commitFeeIntent',True),
    ('owner','WithdrawalFeePayments','prepare',False),('operator','WithdrawalFeeCredits','approveVersion',False),
    ('owner','PoolMpToken','transfer',False),('operator','BitcoinWithdrawalEscrow','cancel',False)])
def test_runtime_limits_actor_and_original_contract_method(role,kind,method,valid):
    r=w.Runtime.__new__(w.Runtime)
    r.owner_records=[]
    ctx={k:NS(address=address(i)) for i,k in enumerate(('registry','fee','pool','common','batch','escrow','board'),1)}
    ctx['withdrawal_fees']={k:NS(address=address(i)) for i,k in enumerate(('payments','funding','credits'),11)}
    auth={k:NS(address=address(i)) for i,k in enumerate(('coordinator','authorization','board'),21)}
    r.graph_check=lambda:(ctx,auth);r.quote=dict(graph=dict(token=address(31)),options=dict(pool=address(32)))
    targets=dict(WithdrawalFeeCredits=address(13),WithdrawalFeePayments=address(11),BitcoinSigningBoard=address(7),
        DKGOnchainBoard=address(23),PoolMpToken=address(31),BitcoinWithdrawalEscrow=address(6))
    profile=dict(contracts=dict(target=dict(address=targets[kind],artifact=kind)),
        steps=[dict(kind='call',method=method,value='0')])
    if valid:
        r.allowed(role,profile);profile['contracts']['target']['address']=address(99)
    with pytest.raises(ValueError,match='CALL_SCOPE'):r.allowed(role,profile)


@pytest.mark.parametrize('change',['dkg','wallet','owner_graph'])
def test_runtime_binds_btc_manifest_to_both_actor_graphs(selected,change):
    from test_address_mint import Functions
    e=selected;r=w.Runtime.__new__(w.Runtime);r.manifest=e.e.manifest;r.owner_records=[]
    ctx={k:NS(address=address(i)) for i,k in enumerate(('registry','fee','pool','common','batch','escrow','board'),1)}
    ctx['block']=NS(number=10);values=dict(dkg=r.manifest.registry,walletId=r.manifest.session_id)
    ctx['registry'].functions=Functions(values)
    ctx['withdrawal_fees']={k:NS(address=address(i)) for i,k in enumerate(('payments','funding','credits'),11)}
    owner=deepcopy(ctx);r.actors=dict(owner=NS(context=lambda:owner),operator=NS(context=lambda:ctx))
    r.need=dict(consumer=ctx['escrow'].address);r.quote=dict(graph=dict(mining=ctx['pool'].address))
    r.driver=NS(authority=lambda ctx:dict(valid=True))
    r.graph_check()
    if change=='dkg':values['dkg']=address(99)
    if change=='wallet':values['walletId']=b'z'*32
    if change=='owner_graph':owner['withdrawal_fees']['credits'].address=address(99)
    with pytest.raises(ValueError,match='GRAPH'):r.graph_check()


def test_reconciliation_cli_runner_chain_keeps_challenge_binding_and_original_report(reconciliation,selected,monkeypatch):
    import subprocess
    from ops.zkspv.contour import Contour
    f=reconciliation;e=selected
    e.rc['deadline']='400';write(e.tmp_path/'reconcile.json',e.rc);f.r.c=e.rc
    plan=contour_plan.build(e.tmp_path/'worker.json',e.tmp_path/'need.json',e.tmp_path/'cli-run',
        reconciliation_config=e.tmp_path/'reconcile.json',worker_only=True)
    plan['stages']=[s for s in plan['stages'] if s['capability']=='reconciliation']
    monkeypatch.setattr(rec,'Runtime',lambda *a:f.r)
    original=subprocess.Popen
    def popen(command,*args,**kwargs):
        if command[1:3]==['-m','ops.zkspv.contour_reconcile']:
            monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',kwargs['env']['SATOHSI_CONTOUR_CONTEXT'])
            rec.main(command[3:]);return NS(pid=999999999,wait=lambda **kw:0)
        return original(command,*args,**kwargs)
    monkeypatch.setattr(subprocess,'Popen',popen)
    runner=Contour(plan)
    for _ in range(8):
        before=len(f.envelopes.raws);result=runner.step();assert len(f.envelopes.raws)-before<=1
        for session in f.sessions.values():session[10]=2  # External threshold outcome fixture.
        if result['status']=='DRIVERS_COMPLETED_UNQUALIFIED':break
    assert result['status']=='DRIVERS_COMPLETED_UNQUALIFIED' and not result['combined_e2e_accepted']
    assert f.envelopes.methods==['createSessionFromDKG','publishIntent','authorizeOperation','publishReconciliation']

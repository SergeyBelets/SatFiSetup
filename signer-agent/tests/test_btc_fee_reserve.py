"""Real native codec/derivation, SQLite and donor crypto; explicit Core/EVM fixtures.

No PoW, deployed quorum, Groth16 or live provider qualification is claimed.
"""
import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import pytest
from coincurve import PrivateKey
from eth_utils import keccak
from signer_agent import btc_fee_reserve as m,btc_fee_policy as policy
from signer_agent.operations import Journal
from signer_agent.bootstrap import Bootstrap
from signer_agent.secure_state import SecureState
from signer_agent.address_watch import Store
from signer_agent.deposit_intent import DepositIntentV1
from signer_agent.bitcoin_proofs import sha256d
from test_address_watch import VECTOR,set_coverage,tx
from test_address_mint import Functions,address
from test_bitcoin_input_keys import signing,native_keys
from test_address_transport import delivery


def node(where,values):return SimpleNamespace(address=where,functions=Functions(values))


@pytest.fixture
def fee_env(tmp_path):
    tmp_path.chmod(0o700);i=object.__new__(m.Inspector)
    intent=DepositIntentV1.decode(bytes.fromhex(VECTOR['intent_bytes']));base=bytes.fromhex(VECTOR['base_group'])
    private=replace(intent,request_nonce=b'z'*32)
    raddr=m.Web3.to_checksum_address(intent.deposit_registry)
    store=Store(tmp_path/'address.sqlite',dict(registry=raddr,chain_id='31337',bitcoin_genesis=intent.bitcoin_genesis.hex()))
    for ordinal,it in enumerate((intent,private)):
        store.accept(it.encode(),base,'regtest',str(ordinal),{'source':'authenticated test request'})
        set_coverage(store,it.intent_id.hex(),7,'77'*32)
    wallet=b'\x51\x20'+base[1:];derived=bytes.fromhex(intent.derive(base)['script_pubkey'])
    private_script=bytes.fromhex(private.derive(base)['script_pubkey'])
    raws=[tx(script,amount,previous='11'*32+':0',tag=n+1) for n,(script,amount) in enumerate(((wallet,1000),(derived,500),(private_script,200)))]
    ids=[sha256d(raw)[::-1].hex() for raw in raws]
    view=SimpleNamespace(cg=1,dg=2,custody=0,tip='77'*32,missing=set(),mempool=set(),pending={},supply=500*10**10,scan=True,pruned=False,
        free=1000,backing=500,report=None,scanned=[],qualification=False)
    rows={display:dict(hex=raw.hex(),blockhash='77'*32) for display,raw in zip(ids,raws)}
    amounts=[1000,500,200];scripts=[wallet,derived,private_script]
    class Core:
        def check_network(self):pass
        def call(self,method,*args):
            if method=='getblockhash':return intent.bitcoin_genesis.hex() if args[0]==0 else view.tip
            if method=='getbestblockhash':return view.tip
            if method=='getblockchaininfo':return dict(blocks=7,initialblockdownload=False,pruned=view.pruned)
            if method=='gettxout':
                if args[0] in view.missing or args[2] and args[0] in view.mempool:return None
                n=ids.index(args[0]);return dict(value=Decimal(amounts[n])/10**8,scriptPubKey=dict(hex=scripts[n].hex()),confirmations=6,coinbase=False)
            if method=='scantxoutset':
                view.scanned=args[1];return dict(success=view.scan,bestblock=view.tip,height=7,unspents=[
                    dict(txid=display,vout=0,scriptPubKey=scripts[n].hex(),amount=Decimal(amounts[n])/10**8) for n,display in enumerate(ids) if display not in view.missing])
            if method=='getrawtransaction':return rows[args[0]]
            if method=='getblockheader':return dict(height=7)
            raise AssertionError(method)
    i.bitcoin=Core();i.c=dict(chain_id='31337',reserve=address(1),watch_databases=[str(store.path)],max_inventory='64',
        stress_sat_per_vbyte='5',service_vbytes='200',future_services='2',low_water_sats='100',target_sats='2000',report_seconds='300')
    i.journal=Journal(tmp_path/'fee.sqlite');i.lane=Journal(tmp_path/'transactions.sqlite')
    block=SimpleNamespace(number=10,hash=b'h'*32,timestamp=100)
    reg_values=dict(bitcoinGenesis=intent.bitcoin_genesis,baseX=base[1:],intentCount=1,intentIds=lambda n:intent.intent_id,
        custodyGeneration=lambda:view.cg,derivationGeneration=lambda:view.dg,custodyState=lambda:view.custody,
        derivationState=1,verifier=address(8),verifierRuntimeHash=keccak(b'fixture'),dkg=address(9),walletId=b'w'*32)
    reg=node(raddr,reg_values)
    common=node(address(2),dict(backingCount=1,backingAt=lambda n:(bytes.fromhex(ids[1])[::-1],0,view.backing),totalBackingSats=lambda:view.backing,depth=6))
    fee_values=dict(creditCount=1,creditAt=lambda n:(bytes.fromhex(ids[0])[::-1],0,view.free,bytes(32),b'b'*32),freeSats=lambda:view.free,
        lockedSats=0,promisedSats=0,protocolFeeCap=1000,maxVbytes=200,reconciliation=lambda:view.report,nonce=0)
    fee=node(address(1),fee_values);pool=node(address(3),dict(mpToken=address(4)));token=node(address(4),dict(totalSupply=lambda:view.supply))
    dkg=node(address(9),dict(getCompletedResult=lambda _:(b'context'.ljust(32,b'\0'),)))
    ctx=dict(fee=fee,registry=reg,common=common,batch=node(address(5),{}),escrow=node(address(6),{}),board=node(address(7),{}),
        pool=pool,block=block,scope='aa'*32,wallet=wallet)
    i.w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda _:block,contract=lambda address,**_:{token.address:token,dkg.address:dkg}[address]))
    i.context=lambda n=None:ctx
    i._known_pending=lambda ctx,display,vout:view.pending.get(display)
    return SimpleNamespace(**locals())


def report(e,result):
    v=result['inventory'];e.view.report=[(0,e.view.cg,e.view.dg,400),1,7,bytes.fromhex(e.view.tip),400,
        *[int(v[k]) for k in ('wallet','backing','pending','free')],bytes.fromhex(result['evidence'])]


def test_complete_inventory_keeps_unminted_private_deposit_out_of_free_and_never_ready(fee_env):
    e=fee_env;r=e.i.reconcile();assert r['status']=='HEALTHY'
    assert [r['inventory'][k] for k in ('wallet','backing','pending','free')]==['1700','500','200','1000']
    assert len(e.view.scanned)==3 and e.private_script.hex() in str(e.view.scanned)
    report(e,r);ctx,again=e.i.healthy();assert again['evidence']==r['evidence']
    status=e.i.status();assert status['status']=='RESERVE_LOW' and status['available_sats']=='1000' and status['runway_sats']=='2000'
    assert status['ready'] is False


@pytest.mark.parametrize('failure',['pruned','scan','tip','coverage'])
def test_unknown_observation_never_creates_free_or_deletes_watch(fee_env,failure):
    e=fee_env
    if failure=='pruned':e.view.pruned=True
    if failure=='scan':e.view.scan=False
    if failure=='tip':e.view.tip='88'*32
    if failure=='coverage':
        with e.store.journal.db() as db:db.execute('UPDATE address_intents SET next_height=0')
    r=e.i.reconcile();assert r['status']=='OBSERVATION_UNAVAILABLE' and not r['ready']
    assert len(e.store.intents())==2 and e.view.free==1000
    assert e.i.inventory(e.ctx) is None


def test_unknown_spend_pauses_common_btc_journal_and_never_auto_clears(fee_env,tmp_path):
    e=fee_env;s=SecureState(str(tmp_path/'signer.enc'),'fixture-state-password')
    e.view.missing.add(e.ids[1]);r=e.i.reconcile(state=s)
    assert r['status']=='RECOVERY_REQUIRED' and r['inventory']['critical']==['MISSING:'+e.ids[1]+':0']
    key='btc-payments-recovery:'+b'context'.ljust(32,b'\0').hex();marker=s.get(key)
    assert marker['kind']=='BTC_FEE_RESERVE'
    e.view.missing.clear();assert e.i.reconcile(state=s)['status']=='HEALTHY'
    assert s.get(key)==marker and not list(tmp_path.glob('*.spent.sqlite'))


def test_supply_mismatch_is_critical_without_principal_write(fee_env):
    e=fee_env;e.view.supply+=1;r=e.i.reconcile()
    assert r['status']=='RECOVERY_REQUIRED' and 'MP_SUPPLY_BACKING_MISMATCH' in r['inventory']['critical']
    assert e.view.backing==500 and e.view.free==1000


def test_approved_mempool_family_remains_collateral_not_new_credit(fee_env):
    e=fee_env;e.view.mempool.update(e.ids[:2])
    for display in e.ids[:2]:e.view.pending[display]=dict(root='cc'*32,confirmed=False)
    r=e.i.reconcile();assert r['status']=='HEALTHY'
    assert r['inventory']['wallet']=='1700' and r['inventory']['free']=='1000'
    assert all(r['inventory']['utxos'][display+':0']['inflight_root']=='cc'*32 for display in e.ids[:2])
    e.fee_values.update(lockedSats=1000);report(e,r)
    assert e.i.status()['available_sats']=='0'


def test_confirmed_own_family_waits_for_settlement_without_theft_classification(fee_env):
    e=fee_env;e.view.missing.update(e.ids[:2])
    for display in e.ids[:2]:e.view.pending[display]=dict(root='cc'*32,confirmed=True)
    r=e.i.reconcile();assert r['status']=='SETTLEMENT_REQUIRED' and r['inventory']['critical']==[]
    assert e.i.inventory(e.ctx) is None and e.view.backing==500


def test_watch_rollback_is_detected_even_for_unregistered_private_intent(fee_env):
    e=fee_env;assert e.i.reconcile()['status']=='HEALTHY'
    with e.store.journal.db() as db:db.execute('DELETE FROM address_intents WHERE id=?',(e.private.intent_id.hex(),))
    assert e.i.reconcile()['status']=='OBSERVATION_UNAVAILABLE'


def test_generation_rollback_cannot_restore_old_fee_permission(fee_env):
    e=fee_env;e.view.cg=3;assert e.i.reconcile()['status']=='HEALTHY'
    e.view.cg=1;assert e.i.reconcile()['status']=='OBSERVATION_UNAVAILABLE'
    with e.i.lane.db() as db:assert db.execute('SELECT cg FROM fee_generations').fetchone()[0]=='3'


def test_evidence_is_independent_of_private_paths_and_duplicate_watch_partitions(fee_env,tmp_path):
    e=fee_env;first=e.i.reconcile()
    backup=tmp_path/'second.sqlite';e.store.backup(backup);e.i.c['watch_databases']=[str(backup),str(e.store.path)]
    second=e.i.reconcile();assert first['evidence']==second['evidence']
    assert str(tmp_path) not in json.dumps(second)


@pytest.mark.parametrize('mutate',['generation','expired','free','evidence'])
def test_stale_report_fails_before_signing(fee_env,mutate):
    e=fee_env;result=e.i.reconcile();report(e,result)
    if mutate=='generation':e.view.cg+=1
    if mutate=='expired':e.view.report[4]=99
    if mutate=='free':e.view.report[8]=999
    if mutate=='evidence':e.view.report[9]=bytes(32)
    with pytest.raises(ValueError,match='BTC_FEE_RECONCILIATION_REQUIRED'):e.i.healthy()


def test_current_custody_pause_does_not_erase_existing_inventory(fee_env):
    e=fee_env;result=e.i.reconcile();report(e,result);e.view.custody=2
    with pytest.raises(ValueError,match='BTC_FEE_GENERATION_CHANGED'):e.i.healthy()
    assert e.i.inventory(e.ctx)['backing']=='500'


@pytest.mark.parametrize('method',['arbitraryCall','setOwner','transfer','getOperationHash'])
def test_generic_fee_calldata_is_not_a_signable_intent(method):
    with pytest.raises(ValueError,match='BTC_FEE_ACTION'):policy.action(method,[])


def test_fee_recovery_marker_cannot_be_cleared_by_ordinary_payment_recovery():
    from signer_agent.bitcoin_onchain import payment_observation,retry_payment_observation
    state=SimpleNamespace(get=lambda _:dict(kind='BTC_FEE_RESERVE',evidence='aa'*32))
    manifest=SimpleNamespace(context=b'c'*32)
    with pytest.raises(ValueError,match='BTC_FEE_RECONCILIATION_RECOVERY_REQUIRED'):
        payment_observation(None,manifest,None,state,dict(batch_id='bb'*32),recover=True)
    with pytest.raises(ValueError,match='BTC_FEE_RECONCILIATION_RECOVERY_REQUIRED'):
        retry_payment_observation(None,manifest,None,state)


def test_bound_fee_policy_cannot_be_bypassed_by_legacy_plan():
    batch=node(address(1),dict(feeReserve=address(2)));registry=SimpleNamespace(w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda **_:batch)))
    with pytest.raises(ValueError,match='BTC_FEE_ADDRESS_PROFILE_REQUIRED'):
        m.guard(None,dict(batch_registry=batch.address,version='2'),registry,{})


def test_journal_loss_is_not_reinitialization(tmp_path,monkeypatch):
    tmp_path.chmod(0o700);identity=tmp_path/'identity.json';identity.write_text('{}');identity.chmod(0o600)
    c=dict(identity_file=str(identity),reserve=address(1),evm_rpc='http://127.0.0.1:8545')
    rpc=object();w3=object();first=m.Inspector(c,w3,rpc);first.journal.path.unlink()
    with pytest.raises(ValueError,match='BTC_FEE_JOURNAL_RECOVERY_REQUIRED'):m.Inspector(c,w3,rpc)


def test_explicit_recovery_requires_new_canonical_report_and_keeps_signing_state(fee_env,tmp_path):
    e=fee_env;s=SecureState(str(tmp_path/'signer.enc'),'fixture-state-password')
    manifest=SimpleNamespace(context=b'context'.ljust(32,b'\0'))
    s.put('btc:immutable-fixture',dict(status='share-prepared',share='public-fixture'))
    e.view.missing.add(e.ids[1]);incident=e.i.reconcile(state=s);e.view.missing.clear()
    with pytest.raises((ValueError,TypeError)):m.recover(e.i,manifest,s)
    current=e.i.reconcile();report(e,current)
    assert m.recover(e.i,manifest,s)['status']=='BTC_FEE_RECOVERED'
    assert s.get('btc-payments-recovery:'+manifest.context.hex())=={}
    assert s.get('btc:immutable-fixture')['status']=='share-prepared'
    assert s.get('btc-fee-recovered:'+e.ctx['scope']+':'+incident['evidence']) is not None


@pytest.mark.parametrize('mutation',['none','signature','private-source','marker'])
def test_native_donation_uses_real_donor_signature_and_never_relabels_deposit(fee_env,mutation):
    from eth_abi import encode
    e=fee_env;donor=PrivateKey((9).to_bytes(32,'big'));source=b'\x51\x20'+donor.public_key.format()[1:]
    if mutation=='private-source':source=e.private_script
    previous=tx(source,2000,previous='11'*32+':0',tag=20);prev_id=sha256d(previous)[::-1].hex()
    raw=tx(e.wallet,1000,previous=prev_id+':0',tag=21)
    if mutation=='marker':
        # Add an OP_RETURN output while preserving the real stripped txid.
        raw=raw[:-4]+bytes(8)+b'\x06\x6a\x04SMP1'+bytes(4)
        raw=bytearray(raw);raw[48]=2;raw=bytes(raw)
    display=sha256d(raw)[::-1].hex()
    message=keccak(encode(['bytes32','bytes32','bytes32','uint32','uint256','bytes32'],[
        keccak(text='SATOHSI_UNCONDITIONAL_FEE_DONATION_V1'),bytes.fromhex(e.ctx['scope']),sha256d(raw),0,1000,keccak(e.wallet)]))
    signature=donor.sign_schnorr(message,aux_randomness=bytes(32))
    if mutation=='signature':signature=signature[:-1]+bytes([signature[-1]^1])
    e.fee_values.update(donationHash=lambda *_:message,fundingVerifier=address(50))
    verifier=node(address(50),dict(verify=lambda *_:(sha256d(raw),1000)))
    old_contract=e.i.w3.eth.contract;e.i.w3.eth.contract=lambda address,**kw:verifier if address==verifier.address else old_contract(address=address,**kw)
    old_call=e.i.bitcoin.call
    def call(method,*args):
        if method=='getrawtransaction' and args[0] in (display,prev_id):return dict(hex=(raw if args[0]==display else previous).hex(),blockhash=e.view.tip)
        if method=='gettxout' and args[0]==display:return dict(value=Decimal('0.00001000'),scriptPubKey=dict(hex=e.wallet.hex()),confirmations=6,coinbase=False)
        return old_call(method,*args)
    e.i.bitcoin.call=call
    funding=(raw,previous,0,signature,bytes.fromhex(e.view.tip)[::-1],[],0)
    if mutation=='none':assert e.i.donation(e.ctx,funding)['message']==message.hex()
    else:
        code={'signature':'BTC_FEE_DONOR_CONSENT','private-source':'BTC_FEE_PROTECTED_SOURCE','marker':'BTC_FEE_DEPOSIT_MARKER'}[mutation]
        with pytest.raises((ValueError,RuntimeError),match=code):e.i.donation(e.ctx,funding)
    assert e.view.free==1000 and e.view.backing==500


def test_typed_action_abi_keeps_nonce_generations_and_bool_canonical():
    from signer_agent.bootstrap import Bootstrap
    wire=[['0','1','2','400'],'0x'+'12'*32,'500','100',False]
    fn,args,data,control=policy.action('allocate',[wire])
    assert control==[0,1,2,400] and args[0][4] is False
    assert policy.action('allocate',Bootstrap.wire(args))[2]==data
    with pytest.raises((ValueError,TypeError)):policy.action('allocate',[[['0','1','2','400'],'0x'+'12'*32,500,'100',False]])


def test_reconciliation_report_requires_exact_independently_observed_evidence(fee_env):
    e=fee_env;observed=e.i.reconcile();report(e,observed);r=deepcopy(e.view.report)
    policy.validate(e.i,e.ctx,'publishReconciliation',[r],r[0],{})
    r[7]=199
    with pytest.raises(ValueError,match='BTC_FEE_REPORT_MISMATCH'):policy.validate(e.i,e.ctx,'publishReconciliation',[r],r[0],{})
    e.view.cg+=1
    with pytest.raises(ValueError,match='BTC_FEE_GENERATION_OR_WINDOW'):policy.validate(e.i,e.ctx,'publishReconciliation',[r],r[0],{})


@pytest.fixture
def fee_driver(fee_env,monkeypatch):
    from eth_abi import encode
    from signer_agent.btc_fee_client import Driver
    from signer_agent import address_registration,spv_automation
    e=fee_env;report(e,e.i.reconcile());e.i.w3.eth.chain_id=31337;e.i.c.update(bitcoin_network='regtest',bitcoin_policy=None,evm_confirmations='1',identity_file='/fixture/identity')
    e.receipts={};e.sessions={};e.public={};e.approved=set();e.transactions={};e.sent=[];e.sequence=0
    auth=node(address(21),dict(isOperationApproved=lambda op,target:op in e.approved))
    board=node(address(22),dict(operator=address(23),canPublishIntent=lambda _:False,intents=lambda sid:e.public.get(sid,b'')))
    coordinator=node(address(24),dict(getSession=lambda sid:e.sessions[sid],sessionDKGId=b'd'*32,sessionDKGResult=b'r'*32,
        getAuthorizationResult=lambda sid:(b'g'*32,e.sessions[sid][3],True,m.ZERO,e.fee.address,2,2,b's'*64)))
    coordinator.events=SimpleNamespace(SessionCreated=lambda:SimpleNamespace(process_receipt=lambda r,**_:r['events']))
    a=dict(authorization=auth,coordinator=coordinator,board=board,dkg_id=b'd'*32,result_hash=b'r'*32,options={})
    monkeypatch.setattr(address_registration,'authority',lambda *_:a)
    monkeypatch.setattr(spv_automation,'identity',lambda _:address(23))
    def helper(selector,data,sequence):
        params=keccak(encode(['bytes32','bytes32'],[bytes.fromhex(e.ctx['scope']),keccak(data)]))
        return keccak(encode(['string','uint256','address','address','bytes4','bytes32','uint256'],[
            'SATOHSI_FROST_AUTHORIZATION_V1',31337,auth.address,e.fee.address,selector,params,sequence]))
    e.fee_values.update(authorization=auth.address,getOperationHash=helper,receipts=lambda op:e.receipts.get(op,bytes(32)),
        nonce=lambda:e.sequence,available=1000,allocations=lambda _:(0,0,False,False,False,bytes(32)))
    bundle=dict(onchain_board=board.address)
    def send(self,key,kind,target,method,args,guard):
        e.sent.append((key,method))
        if key in e.transactions:return e.transactions[key]
        guard()
        if method=='createSessionFromDKG':
            sid=len(e.sessions)+1;e.sessions[sid]=[sid,address(23),b'g'*32,args[1],True,2,2,args[3],True,m.ZERO,1,0,0,0,0,e.fee.address]
            result=dict(blockNumber=10,blockHash=b'h'*32,events=[dict(args=dict(sessionId=sid))])
        elif method=='publishIntent':e.public[int(args[0])]=args[1];result={}
        elif method=='authorizeOperation':e.approved.add(args[1]);result={}
        elif method=='allocate':
            data=policy.action('allocate',Bootstrap.wire(args[:-1]))[2]
            e.receipts[args[-1]]=keccak(data);e.sequence+=1;result={}
        else:raise AssertionError(method)
        e.transactions[key]=result;return result
    monkeypatch.setattr(Driver,'send',send)
    e.driver=Driver(e.i,bundle);e.bundle=bundle;e.authority=a
    return e


def prepare_grant(e):return e.driver.prepare('allocate',[[['0','1','2','400'],'0x'+'12'*32,'100','100',False]])['request']


def test_typed_frost_proposal_restart_and_exact_receipt_completion(fee_driver):
    e=fee_driver;request=prepare_grant(e);intent=e.driver.propose(request)['intent']
    assert e.driver.propose(request)['intent']==intent and len(e.sessions)==1
    e.sessions[1][10]=2;assert e.driver.execute(intent)['status']=='BTC_FEE_EXECUTED'
    count=len(e.sent);e.view.cg+=1;e.view.custody=2;e.block.timestamp=1000
    assert e.driver.execute(intent)['status']=='BTC_FEE_EXECUTED' and len(e.sent)==count


@pytest.mark.parametrize('foreign_creator',[False,True])
def test_fee_session_accepts_only_board_delegated_custodian(fee_driver,foreign_creator):
    e=fee_driver
    e.authority['board'].functions.values.update(operator=address(90),canPublishIntent=lambda who:who==address(23))
    intent=e.driver.propose(prepare_grant(e))['intent']
    assert policy.review(e.i.w3,intent,e.bundle,10,inspector=e.i)['session']==1
    assert not e.approved and not e.receipts
    if foreign_creator:
        e.sessions[1][1]=address(99)
        with pytest.raises(ValueError,match='BTC_FEE_SESSION_MISMATCH'):policy.review(e.i.w3,intent,e.bundle,10,inspector=e.i)


@pytest.mark.parametrize('mutate',['generation','publication','creator','hash','receipt'])
def test_fee_intent_cannot_substitute_generation_authority_or_consumed_only_status(fee_driver,mutate):
    e=fee_driver;intent=e.driver.propose(prepare_grant(e))['intent']
    if mutate=='generation':e.view.cg+=1
    elif mutate=='publication':e.public[1]=b'{}'
    elif mutate=='creator':e.sessions[1][1]=address(99)
    elif mutate=='hash':intent['hash_args'][1]='0x'+bytes(32).hex()
    else:e.receipts[bytes.fromhex(intent['operation_hash'])]=b'wrong'.ljust(32,b'\0')
    with pytest.raises(ValueError,match='BTC_FEE_'):e.driver.execute(intent)
    assert 'authorizeOperation' not in [m for _,m in e.sent] and 'allocate' not in [m for _,m in e.sent]


def test_fee_proposal_lost_session_receipt_reuses_identical_transaction(fee_driver,monkeypatch):
    e=fee_driver;request=prepare_grant(e);update=e.i.journal.update
    monkeypatch.setattr(e.i.journal,'update',lambda *_:(_ for _ in ()).throw(RuntimeError('DISK_CRASH')))
    with pytest.raises(RuntimeError,match='DISK_CRASH'):e.driver.propose(request)
    assert len(e.sessions)==1
    monkeypatch.setattr(e.i.journal,'update',update)
    assert e.driver.propose(request)['intent']['session']=='1' and len(e.sessions)==1


@pytest.mark.parametrize('condition,expected',[('healthy','1'),('missing','3'),('unavailable','2')])
def test_reconciliation_driver_prepares_healthy_or_suspension_without_funding(fee_driver,condition,expected):
    e=fee_driver
    if condition=='missing':e.view.missing.add(e.ids[0])
    if condition=='unavailable':e.view.pruned=True
    request=e.driver.prepare_reconciliation('400')['request']
    report=request['metadata']['args'][0]
    assert report[1]==expected and not e.sent and e.view.free==1000
    if condition=='unavailable':assert report[2]=='0' and report[5:9]==['0']*4


def test_fee_config_is_required_before_any_bitcoin_nonce_or_cached_export(signing):
    from signer_agent import bitcoin_worker,bitcoin_onchain
    e=signing;e.batch_values['feeReserve']=address(60)
    with pytest.raises(ValueError,match='BTC_FEE_CONFIG_REQUIRED'):
        bitcoin_worker.sign_step('btc-commit',e.manifest,e.key,e.plan,e.registry,e.states[0],e.bitcoin)
    sid='btc:'+e.manifest.context.hex()+':'+e.plan['batch_id']+':0'
    assert e.states[0].get(sid) is None
    e.batch_values['feeReserve']=address(0)
    bitcoin_worker.sign_step('btc-commit',e.manifest,e.key,e.plan,e.registry,e.states[0],e.bitcoin)
    saved=e.states[0].get(sid);e.batch_values['feeReserve']=address(60)
    with pytest.raises(ValueError,match='BTC_FEE_CONFIG_REQUIRED'):
        bitcoin_onchain.cached_transport_step('btc-commit',e.manifest,e.key,e.plan,e.registry,e.states[0],e.bitcoin)
    assert e.states[0].get(sid)==saved


def test_native_prepared_plan_guard_binds_hash_fee_reservation_and_current_generations(signing,monkeypatch):
    from signer_agent.bitcoin_input_keys import build
    from signer_agent.bitcoin_onchain_plan import onchain_plan_hash
    e=signing;e.batch_values['feeReserve']=address(60);h=onchain_plan_hash(e.manifest,e.plan)
    p=[b'a'*32,b'b'*32,bytes(32),h,sha256d(bytes.fromhex(build(e.plan)['unsigned'])),b'i'*32,b'r'*32,
        1000,1000,1000,5000,1,2,True,False]
    fee=node(address(60),dict(getPlan=lambda _:p,protocolFeeCap=10000,
        allocations=lambda _:(2000,2000,False,True,True,b'b'*32),winners=lambda _:bytes(32),checkSigning=lambda _:None))
    ctx=dict(fee=fee,batch=e.batches,registry=e.reg,block=e.block)
    i=object.__new__(m.Inspector);i.w3=e.w3;i.healthy=lambda **_: (ctx,dict(inventory=dict(tip='77'*32)))
    monkeypatch.setattr(m,'configured',lambda *args,**kwargs:i)
    assert m.guard(e.manifest,e.plan,e.registry,e.graph,bitcoin=e.bitcoin)
    p[13]=False
    with pytest.raises(ValueError,match='BTC_FEE_PREPARED_PLAN_REQUIRED'):m.guard(e.manifest,e.plan,e.registry,e.graph)
    assert m.guard(e.manifest,e.plan,e.registry,e.graph,reserved=False)
    p[13]=True;e.v.cg=2
    with pytest.raises(ValueError,match='BTC_FEE_GENERATION_CHANGED'):m.guard(e.manifest,e.plan,e.registry,e.graph)


def test_address_service_v2_retains_watch_and_reports_fee_status_without_ready(delivery,monkeypatch):
    from signer_agent import address_transport,address_mint
    e=delivery;e.requester.request(e.intent.encode(),e.accounts[1].address);e.publish(e.requester);e.service.service_once()
    before=e.service.store.intents();config=dict(e.configs[1],version='2',fee_reserve_config='/private/fee.json')
    fc={k:config[k] for k in ('chain_id','bitcoin_network','bitcoin_policy','evm_confirmations','identity_file')};fc['reserve']=address(60)
    monkeypatch.setattr(m,'load_config',lambda _:fc)
    monkeypatch.setattr(m,'Inspector',lambda *args:SimpleNamespace(context=lambda:dict(registry=e.reg,pool=e.pool),
        status=lambda **_:dict(status='RESERVE_LOW',available_sats='0',ready=False)))
    client=address_transport.Client(config,e.w3);assert client.store.intents()==before
    e.reg_values['getBinding']=lambda _:(b'fixture registered',)
    monkeypatch.setattr(address_mint,'binding',lambda *_:dict(intent=e.intent))
    set_coverage(client.store,e.intent.intent_id.hex(),7,'77'*32)
    core=SimpleNamespace(check_network=lambda:None,call=lambda method,*args:e.intent.bitcoin_genesis.hex() if method=='getblockhash' and args[0]==0 else '77'*32)
    e.view.derivation=0
    status=client.status(e.intent.intent_id.hex(),core)
    assert status['status']=='RESERVE_LOW' and status['fee_reserve']['available_sats']=='0' and not status['ready']
    assert client.fee_status(full=True)['status']=='RESERVE_LOW'
    covered=client.store.intents()
    with pytest.raises(ValueError,match='BTC_FEE_CONFIG_REQUIRED'):address_transport.Client(e.configs[1],e.w3)
    assert client.store.intents()==covered

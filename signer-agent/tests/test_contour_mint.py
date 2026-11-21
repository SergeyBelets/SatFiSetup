"""Original mint SDK, typed ABI/EOA/SQLite control; graph/Core/FROST/EVM effects are fixtures."""
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

from ops.zkspv import contour_mint as m,contour_member as member
from ops.zkspv.contour import digest
from signer_agent import bitcoin_onchain,bitcoin_funding,custody_mint,address_mint,mint_admission
from signer_agent.bootstrap import Bootstrap,artifact
from signer_agent.operations import Journal
from signer_agent.zk_worker import Inbox,Worker
from signer_agent.zk_worker_prover import write
from test_mint_admission import env
from test_address_mint_admission import driver,generated
from test_contour_worker import pipeline
from test_smp1_deposit import client
from test_zk_worker import config

ORIGINAL_SEND=bitcoin_onchain.send


@pytest.fixture(params=['SMP1_V1','SMP1_V2','ADDRESS'])
def mint(request,tmp_path,monkeypatch):
    mode=request.param;d=request.getfixturevalue('driver') if mode=='ADDRESS' else None;g=d.g if d else None
    e=g.e if g else request.getfixturevalue('env')
    wc=config(tmp_path);wc['allowed_consumers']={e.names['pool']:'MiningPoolV1'}
    w=Worker.__new__(Worker);w.c=wc;w.inbox=Inbox(wc);w.lock_fd=None
    identity=tmp_path/'caller.json';write(identity,dict(private_key=(1).to_bytes(32,'big').hex()))
    c=dict(version='1',mode='ADDRESS' if g else 'SMP1',identity_file=str(identity),mint_admission=e.names['board'],deadline='1000',
        budget=dict(max_tx_gas='2000000',max_transaction_cost_wei='2000000',max_total_cost_wei='20000000',
                    max_job_cost_wei='10000000',max_attempts_per_job='8'))
    need=dict(kind='address_deposit' if g else 'deposit',consumer=e.names['pool'],txid=e.arguments[0][::-1].hex(),vout=str(e.arguments[1]))
    if g:need['intent_id']=g.claim[0].hex()
    graph=d.graph if d else e.graph
    graph.update(version='2',bitcoin_network='mainnet',bitcoin_policy=wc['bitcoin_policy'],evm_confirmations='1')
    e.registry.identity_file=str(identity);e.registry.transaction_journal=str(identity)+'.transactions.sqlite'
    proof=[b'raw',e.arguments[1],b'b'*32,[],0,e.arguments[2],e.arguments[3]]
    monkeypatch.setattr(bitcoin_funding,'_mint_proof',lambda *a:deepcopy(proof))
    if g:
        old=address_mint.funding
        monkeypatch.setattr(address_mint,'funding',lambda *a,**kw:(old(*a,**kw)[0],[b'raw',b'b'*32,[],0]))
        def dispatch(action):
            return d.dispatch('btc-address-mint-'+action)
    else:
        def dispatch(action):
            a=NS(action='btc-mint-'+action,deadline=1000,mint_admission=e.names['board'])
            if mode=='SMP1_V2':return bitcoin_funding.dispatch(a,NS(),dict(result=dict(group='02'+'44'*32)),e.registry,e.state,NS())
            return e.dispatch(a.action)
    if mode=='SMP1_V2':
        from test_address_mint import Functions,address
        e.data['pool']['mintProofVersion']=2;e.data['board']['claimModes']=1;cg=NS(value=1,status=0)
        r=NS(address=address(80),functions=Functions(dict(custodyScope=b's'*32,custodyGeneration=lambda:cg.value,
            custodyState=lambda:cg.status,requireCustody=lambda value:None if value==cg.value and cg.status in (0,1)
                else (_ for _ in ()).throw(ValueError('BTC_MINT_GENERATION_CHANGED')))))
        ac=dict(registry=r,pool=e.contracts['pool'],number=123)
        monkeypatch.setattr(address_mint,'configuration',lambda *a:ac)
        graph['contracts'].update(address_registry=dict(address=r.address))
        e.contracts['deposits']=NS(functions=Functions(dict(consumed=lambda *_:e.view.complete)))
        monkeypatch.setattr(custody_mint,'confirmed_block',lambda *a:e.block(123))
        monkeypatch.setattr(custody_mint,'check_bitcoin_graph',lambda *a:None)
        monkeypatch.setattr('signer_agent.bitcoin_onchain_payment._bitcoin',lambda *a:object())
        monkeypatch.setattr('signer_agent.bitcoin_continuity.trust_guard',lambda *a,**kw:None)
        monkeypatch.setattr(custody_mint,'_mint_proof',lambda *a:deepcopy(proof))
        monkeypatch.setattr(custody_mint,'_mint_complete',lambda *a:e.view.complete)
        monkeypatch.setattr(bitcoin_funding,'_graph',lambda *a:(graph,e.contracts))
        monkeypatch.setattr(bitcoin_funding,'_mining',lambda *a:e.contracts['pool'])
        monkeypatch.setattr(bitcoin_funding,'_mint_args',lambda *a:e.arguments)
        monkeypatch.setattr(bitcoin_funding,'confirmed_block',lambda *a:e.block(123))
        base_hash=mint_admission._operation_hash;base_read=mint_admission._read
        def context():return dict(e.context(),address_context=ac)
        def typed_hash(ctx,kind,method,args,nonce):
            if method=='mintFromProof':return base_hash(context(),'MiningPoolV1','mintFromProofV2',[*args,cg.value],nonce)
            return base_hash(ctx,kind,method,args,nonce)
        monkeypatch.setattr(mint_admission,'_operation_hash',typed_hash)
        def read(node,n,method,*args):
            if method=='claimModes':return 1
            if method=='attemptFences':return [1,0]
            return base_read(node,n,method,*args)
        monkeypatch.setattr(mint_admission,'_read',read)
        original=e.contracts['pool'].functions
        class PoolFunctions:
            def __getattr__(self,name):
                if name=='getCustodyMintOperationHash':return lambda *a:NS(call=lambda **kw:e.digest())
                if name=='mintedAddressIntents':return lambda *a:NS(call=lambda **kw:getattr(e.view,'address_intent',bytes(32)))
                return getattr(original,name)
        e.contracts['pool'].functions=PoolFunctions();e.cg=cg
    e.view.absent=True
    e.data['coordinator']['getAuthorizationResult']=[b'group',e.digest(),True,'0x'+'00'*20,e.names['pool'],2,2,b'signature']
    runtime=NS(registry=e.registry,dispatch=dispatch,targets=lambda:{'BitcoinMintAdmission':e.names['board'],
        'MiningPoolV1':e.names['pool'],'DKGAuthorization':e.names['auth']})
    e.w,e.c,e.need,e.mode,e.runtime=w,c,need,mode,runtime;e.transactions={};e.receipts={};e.raws=[];e.crash=None;e.current_ready=True
    e.generation=g.view if g else None
    old_eth=e.w3.eth
    def receipt(txid):
        if txid not in e.receipts:raise TransactionNotFound(txid)
        return e.receipts[txid]
    def transaction(txid):
        if txid not in e.transactions:raise TransactionNotFound(txid)
        return e.transactions[txid]
    def send_raw(raw):
        tx=Transaction.from_bytes(raw).as_dict();txid=Web3.keccak(raw).hex()
        if txid in e.transactions:return bytes.fromhex(txid)
        assert tx['nonce']==len(e.transactions)
        target=Web3.to_checksum_address(bytes(tx['to']).hex());kind=next(k for k,a in runtime.targets().items() if a==target)
        contract=Web3().eth.contract(address=target,abi=artifact(kind)['abi']);fn,args=contract.decode_function_input(tx['data'])
        method=fn.fn_name;e.view.sends.append((method,deepcopy(args)))
        if method in ('openMint','openCustodyMint','openAddressMint'):
            e.view.session+=1;e.view.state=1;e.view.absent=False;e.data['board']['slotSession']=e.view.session
        elif method=='authorizeOperation':e.view.approved=True
        elif method in ('mintFromProof','mintFromProofV2','mintFromAddressProof'):e.view.complete=True
        e.transactions[txid]=tx;e.raws.append(raw)
        e.receipts[txid]=AttributeDict(dict(status=1,blockNumber=123,blockHash=b'b'*32,transactionHash=bytes.fromhex(txid),logs=[]))
        return bytes.fromhex(txid)
    e.w3.eth=NS(chain_id=31337,block_number=200,get_block=e.block,contract=old_eth.contract,get_code=lambda *a,**kw:b'fixture-code',
        get_transaction_count=lambda *a:len(e.transactions),get_transaction=transaction,get_transaction_receipt=receipt,
        send_raw_transaction=send_raw,wait_for_transaction_receipt=lambda txid,**kw:receipt(txid))
    e.w3.provider=NS(endpoint_uri='http://127.0.0.1:1')
    monkeypatch.setattr(bitcoin_onchain,'send',ORIGINAL_SEND)
    def ready(self):
        if not e.current_ready:raise ValueError('CONTOUR_MINT_CURRENT_MEMBERSHIP_REQUIRED')
    monkeypatch.setattr(m.Pipeline,'ready',ready)
    def runner(owner,profile):
        b=Bootstrap.__new__(Bootstrap);b.profile=profile;b.prefix=profile['deployment_id'];b.w3=e.w3
        b.journal=Journal(e.registry.transaction_journal);b.journal.put(b.prefix,dict(profile=profile,version='1'))
        b.contracts=profile['contracts'];b.values={};b.check_external_components=lambda:None
        b.contract=lambda name:Web3().eth.contract(address=profile['contracts'][name]['address'],abi=artifact(profile['contracts'][name]['artifact'])['abi'])
        b.manifest=lambda:dict(deployment_id=b.prefix)
        def build(step,account,nonce):
            if e.crash=='unsigned':raise RuntimeError('unsigned contour mint fixture')
            tx=dict(chainId=31337,nonce=nonce,to=profile['contracts']['target']['address'],gas=200000,gasPrice=1,value=0,
                    data=b.fn(step)._encode_transaction_data())
            owner.budget.reserve(b.prefix,tx);return tx
        b.build_transaction=build
        return b
    monkeypatch.setattr(m.Pipeline,'runner',runner)
    def execute(action='probe'):
        ctx=dict(capability='address_mint' if g else 'smp1_mint')
        e.controller=m.Pipeline(w,need,c,ctx,runtime)
        return e.controller.execute(action)
    e.execute=execute
    yield e
    w.inbox.close()


def complete(e):
    assert e.execute()['status']=='ABSENT'
    assert e.execute('run')['status']=='WAITING'
    e.view.state=2
    for _ in range(8):
        result=e.execute('recover')
        if result['status']=='SATISFIED':return result
    raise AssertionError(result)


def test_mint_runner_uses_original_admission_authorization_and_exact_receipt(mint):
    e=mint;result=complete(e)
    assert result['status']=='SATISFIED' and not result['observations']['ready']
    assert [s[0] for s in e.view.sends]==([
        'openAddressMint' if e.mode=='ADDRESS' else 'openCustodyMint' if e.mode=='SMP1_V2' else 'openMint',
        'authorizeOperation','mintFromAddressProof' if e.mode=='ADDRESS' else 'mintFromProofV2' if e.mode=='SMP1_V2' else 'mintFromProof'])
    raw=list(e.raws);assert e.execute()['status']=='SATISFIED' and e.raws==raw


def test_typed_mint_profile_rejects_current_version_mismatch_before_new_signature(mint):
    e=mint;e.execute()
    expected='1' if e.mode=='SMP1_V1' else '2';e.runtime.expected_proof_version=expected
    e.data['pool']['mintProofVersion']=2 if expected=='1' else 1
    with pytest.raises(ValueError,match='PROOF_PROFILE'):e.controller.check_proof_version()
    assert not e.raws
    e.data['pool']['mintProofVersion']=int(expected);e.controller.check_proof_version()


def test_historical_exact_mint_profile_survives_later_pool_version_switch(mint):
    e=mint;e.runtime.expected_proof_version='1' if e.mode=='SMP1_V1' else '2'
    complete(e);original=list(e.raws)
    e.data['pool']['mintProofVersion']=2 if e.mode=='SMP1_V1' else 1
    assert e.execute()['status']=='SATISFIED' and e.raws==original
    e.runtime.expected_proof_version='2' if e.mode=='SMP1_V1' else '1'
    with pytest.raises(ValueError,match='PROOF_PROFILE'):e.execute()
    assert e.raws==original


def test_mint_runner_never_finalizes_frost_or_retries_a_session(mint):
    e=mint;assert e.execute('run')['status']=='WAITING'
    for _ in range(3):assert e.execute()['status']=='WAITING'
    assert len(e.raws)==1 and e.view.state==1


def test_mint_unsigned_interruption_is_a_hold_without_another_nonce(mint):
    e=mint;e.crash='unsigned'
    with pytest.raises(RuntimeError,match='unsigned'):e.execute('run')
    e.crash=None
    assert e.execute('recover')['status']=='RECOVERY_REQUIRED' and not e.raws


def test_mint_recovers_signed_operation_before_new_current_funding_checks(mint,monkeypatch):
    e=mint;original=Journal.update
    def update(journal,key,status,result):
        if journal.path==Path(e.registry.transaction_journal) and status=='SIGNED':raise RuntimeError('global signature retained')
        return original(journal,key,status,result)
    monkeypatch.setattr(Journal,'update',update)
    with pytest.raises(RuntimeError,match='global signature'):e.execute('run')
    monkeypatch.setattr(Journal,'update',original);e.current_ready=False
    assert e.execute('recover')['status']=='ABSENT' and len(e.raws)==1


def test_mint_receipt_reorg_is_not_replaced_by_a_completion_marker(mint):
    e=mint;complete(e);key=next(iter(e.receipts));e.receipts[key]=dict(e.receipts[key],blockHash=b'z'*32)
    e.receipts[key]=AttributeDict(e.receipts[key])
    with pytest.raises(ValueError,match='REORG'):e.execute()


def test_mint_fee_caps_are_enforced_before_signature(mint):
    e=mint;e.c['budget']['max_transaction_cost_wei']='100'
    with pytest.raises(ValueError,match='TX_BUDGET'):e.execute('run')
    assert not e.raws


def test_mint_journal_restart_reuses_original_sessions_and_transactions(mint):
    e=mint;complete(e);raw=list(e.raws);e.w.inbox.close();e.w.inbox=Inbox(e.w.c)
    assert e.execute()['status']=='SATISFIED' and e.raws==raw


def test_another_valid_signed_envelope_cannot_replace_original_mint_operation(mint):
    e=mint;e.execute('run');profile=next(iter(e.controller.record['operations'].values()))
    key=profile['deployment_id']+':send';journal=Journal(e.registry.transaction_journal);entry=journal.get(key)
    account=Account.from_key((1).to_bytes(32,'big'))
    raw=account.sign_transaction(dict(chainId=31337,nonce=0,to=e.names['board'],gas=200000,gasPrice=1,value=0,data='0x12345678')).raw_transaction
    journal.update(key,'SIGNED',dict(entry['result'],raw_transaction=raw.hex(),tx_hash=Web3.keccak(raw).hex()))
    before=list(e.raws)
    with pytest.raises(ValueError,match='CALL_ENVELOPE'):e.execute('recover')
    assert e.raws==before


@pytest.mark.parametrize('mint',['SMP1_V2'],indirect=True)
def test_smp1_v2_cannot_adopt_an_address_mint_completion(mint):
    e=mint;complete(e);e.view.address_intent=b'a'*32
    with pytest.raises(ValueError,match='COMPLETION_MODE'):e.execute()


@pytest.mark.parametrize('mint',['SMP1_V2'],indirect=True)
def test_smp1_v2_requests_state_before_live_freshness_and_paid_mint_work(mint,monkeypatch):
    e=mint;seen=[]
    monkeypatch.setattr(bitcoin_funding,'consumer_readiness',lambda *a:seen.append(a) or dict(status='NEEDS_STATE'))
    monkeypatch.setattr(custody_mint,'check_bitcoin_graph',lambda *a:pytest.fail('request state before live freshness'))
    assert e.runtime.dispatch('propose')['status']=='AWAITING_ZK_STATE' and len(seen)==1 and not e.raws


@pytest.mark.parametrize('mint',['SMP1_V2'],indirect=True)
def test_smp1_v2_suspended_custody_blocks_new_admission(mint):
    e=mint;e.cg.status=2
    with pytest.raises(ValueError,match='GENERATION_CHANGED'):e.execute('run')
    assert not e.raws


def test_generation_change_does_not_create_an_automatic_retry(mint):
    e=mint
    if e.mode=='SMP1_V1':return
    e.execute('run');e.view.state=2
    if e.mode=='SMP1_V2':e.cg.value=2
    else:e.generation.cg+=1
    with pytest.raises(ValueError):e.execute('recover')
    assert len(e.raws)==1


@pytest.fixture
def materialization(pipeline):
    e=pipeline;w=e.worker;e.current=list(e.after);e.current[3]='32262';e.fresh=True;w.aligned=lambda *a:True
    ctx=w.context;actual=Web3().eth.contract(address=Account.from_key((2).to_bytes(32,'big')).address,abi=artifact('ZkSpvAdapter')['abi'])
    e.cache=[bytes(32),bytes(32),0];e.member_calls=0;e.member_raw=None;e.member_crash=None;e.events=[]
    member_value=dict(height='32257',header=w.observe_need(None)['header'],siblings=['00'*32]*64)
    def call_member(*args):
        assert len(args[2])==64
        return NS(call=lambda **kw:bytes(32))
    adapter=NS(address=actual.address,functions=NS(materializedHeaders=lambda key:NS(call=lambda **kw:e.cache),
        materializeHeader=call_member),events=NS(HeaderMaterialized=lambda:NS(process_receipt=lambda *a,**kw:e.events)))
    w.context=lambda **kw:(*ctx(**kw)[:2],adapter,dict(spv_guard=dict(address=actual.address,artifact='ZkSpvAdapter')),dict(blocks=32262))
    w.inbox.put('archive:'+e.current[7],[])
    def native(*args):e.member_calls+=1;return dict(before=e.current,memberships=[member_value])
    w.native=native
    def publish(name,step,records,*,recover_only=False):
        journal=Journal(w.inbox.path/'transactions.sqlite');entry=journal.put(name+':header',step)
        if not entry['result'].get('raw_transaction'):
            if recover_only:return dict(status='NOT_SIGNED')
            if e.member_crash=='unsigned':raise RuntimeError('unsigned member interruption')
            data=actual.functions.materializeHeader(32257,bytes.fromhex(member_value['header']),[bytes(32)]*64)._encode_transaction_data()
            signed=e.account.sign_transaction(dict(chainId=31337,nonce=0,to=actual.address,gas=200000,gasPrice=1,value=0,data=data))
            e.member_raw=bytes(signed.raw_transaction);txid=Web3.keccak(e.member_raw).hex()
            journal.update(name+':header','SIGNED',dict(raw_transaction=e.member_raw.hex(),tx_hash=txid,nonce='0'))
            if e.member_crash=='signed':raise RuntimeError('signed member interruption')
        entry=journal.get(name+':header');txid=entry['result']['tx_hash'];e.receipts[txid]=AttributeDict(dict(status=1,
            blockNumber=10,blockHash=b'h'*32,transactionHash=bytes.fromhex(txid.removeprefix('0x'))))
        result=dict(entry['result'],block_hash=(b'h'*32).hex(),block_number='10');journal.update(name+':header','CONFIRMED',result)
        from signer_agent.bitcoin_network import header_hash
        e.events=[dict(address=actual.address,args=dict(blockHash=header_hash(bytes.fromhex(member_value['header'])),height=32257,canonicalRoot=bytes.fromhex(e.current[6])))]
        e.cache=[bytes.fromhex(e.current[6]),bytes.fromhex(member_value['header'])[36:68],32257]
        return dict(status='VERIFIED')
    w.publish=publish
    def execute(action='probe'):return member.Pipeline(w,e.request,dict(capability='membership',run_id='aa'*32,plan_sha256='bb'*32)).execute(action)
    e.member=execute
    return e


def test_membership_uses_native_archive_without_a_new_proof_and_recovers_receipt(materialization):
    e=materialization
    assert e.member()['status']=='ABSENT'
    assert e.member('run')['observations']['reason']=='EXACT_MEMBERSHIP_PREPARED'
    assert e.member('recover')['status']=='ABSENT'
    assert e.member()['status']=='SATISFIED' and e.member_calls==1 and e.stages==[]


@pytest.mark.parametrize('crash',['unsigned','signed'])
def test_membership_interruption_preserves_original_nonce_and_proof(materialization,crash):
    e=materialization;e.member('run');e.member_crash=crash
    with pytest.raises(RuntimeError):e.member('recover')
    raw=e.member_raw;e.member_crash=None;result=e.member('recover')
    assert result['status']==('RECOVERY_REQUIRED' if crash=='unsigned' else 'ABSENT')
    assert e.member_raw==raw and e.member_calls==1 and e.stages==[]


def test_membership_stale_root_does_not_start_a_second_materialization(materialization):
    e=materialization;e.member('run');e.current[6]='66'*32
    assert e.member('recover')['observations']['reason']=='MEMBERSHIP_ROOT_CHANGED'
    assert e.member_calls==1 and not e.member_raw


def test_membership_confirmed_receipt_loss_is_a_hold(materialization):
    e=materialization;e.member('run');e.member('recover');assert e.member()['status']=='SATISFIED'
    e.receipts.clear();assert e.member()['status']=='RECOVERY_REQUIRED'


def test_cached_membership_adoption_does_not_bypass_finality_regression(materialization):
    e=materialization;header=bytes.fromhex(e.worker.observe_need(None)['header'])
    e.cache=[bytes.fromhex(e.current[6]),header[36:68],32257]
    assert e.member()['status']=='SATISFIED' and e.member_calls==0
    old=e.worker.context
    def context(**kw):
        b,*tail=old(**kw);return (AttributeDict(dict(b,number=9)),*tail)
    e.worker.context=context
    assert e.member()['status']=='RECOVERY_REQUIRED'


@pytest.mark.parametrize('corrupt',[False,True])
def test_already_accepted_archive_retention_never_starts_proving_or_publication(pipeline,corrupt):
    e=pipeline;base=e.prepared();active=e.worker.inbox.get('attempt')
    if corrupt:(base/'native.json').write_text('{}')
    native=json.loads((base/'native.json').read_text())
    if corrupt:
        with pytest.raises((ValueError,KeyError)):e.worker.retain_accepted_archive(active,native,e.after,e.worker.context()[0])
    else:
        e.worker.retain_accepted_archive(active,native,e.after,e.worker.context()[0])
        assert e.worker.inbox.get('attempt') is None and e.worker.inbox.get('archive:'+e.after[7])==native['archive']
    assert e.stages==[] and e.publishes==0


def test_smp1_recipient_review_detects_code_added_after_accepted_quote(client):
    d,quote,_,_=client;r=m.Runtime.__new__(m.Runtime);r.c=dict(mode='SMP1');r.deposit=d;r.quote=quote()['quote']
    d.w3.eth.get_code=lambda *a,**kw:b''
    r.review_recipient()
    d.w3.eth.get_code=lambda *a,**kw:b'new-code' if kw['block_identifier']=='latest' else b''
    with pytest.raises(ValueError,match='RECIPIENT_PROFILE'):r.review_recipient()


def test_plan_builder_wires_membership_and_pins_address_mint_inputs(tmp_path):
    from ops.zkspv.contour_plan import build
    from test_dkg_protocol import fixture
    wc=config(tmp_path);need=dict(kind='address_deposit',consumer=next(iter(wc['allowed_consumers'])),intent_id='11'*32,txid='22'*32,vout='0')
    bundle=fixture()['manifest'];bundle.update(chain_id='31337',purpose='BTC_SPEND')
    mc=json.loads(Path('config/zkspv-contour-address-mint.example.json').read_text());mc['bundle_file']=str(tmp_path/'bundle.json')
    for name,value in [('worker.json',wc),('need.json',need),('mint.json',mc),('bundle.json',bundle)]:write(tmp_path/name,value)
    plan=build(tmp_path/'worker.json',tmp_path/'need.json',tmp_path/'run',mint_config=tmp_path/'mint.json',consumer_only=True)
    assert [s['capability'] for s in plan['stages']]==['membership','address_mint']
    assert 'ops.zkspv.contour_member' in plan['stages'][0]['run'] and digest(mc) in plan['stages'][1]['run']
    assert digest([bundle,None,None]) in plan['stages'][1]['run']


@pytest.mark.parametrize('mint',['ADDRESS'],indirect=True)
def test_runner_cli_chain_adopts_membership_and_executes_original_address_mint(mint,tmp_path,monkeypatch):
    import subprocess
    from ops.zkspv.contour import Contour
    from ops.zkspv.contour_plan import build
    from test_dkg_protocol import fixture
    from test_zk_worker import fake_state,fixture_headers
    e=mint;raw=fixture_headers()[32257];root='11'*32
    current=['00'*32,'32256','00'*32,'32262','01'*32,'1500000000',root,'22'*32,False,'33'*32,'44'*32,'7']
    state=NS(functions=NS(getState=lambda:NS(call=lambda **kw:fake_state(current)),isFresh=lambda:NS(call=lambda **kw:True)))
    adapter=NS(functions=NS(materializedHeaders=lambda *_:NS(call=lambda **kw:[bytes.fromhex(root),raw[36:68],32257])))
    e.w.w3=e.w3;e.w.context=lambda **kw:(e.block(123),state,adapter,{},dict(blocks=32262))
    e.w.observe_need=lambda *a:dict(status='READY',height=32257,header=raw.hex(),depth=6);e.w.aligned=lambda *a:True
    mc=json.loads(Path('config/zkspv-contour-address-mint.example.json').read_text());mc.update(e.c);mc['bundle_file']=str(tmp_path/'bundle.json')
    bundle=fixture()['manifest'];bundle.update(chain_id='31337',purpose='BTC_SPEND')
    for name,value in [('worker.json',e.w.c),('need.json',e.need),('mint.json',mc),('bundle.json',bundle)]:write(tmp_path/name,value)
    plan=build(tmp_path/'worker.json',tmp_path/'need.json',tmp_path/'run',mint_config=tmp_path/'mint.json',consumer_only=True)
    monkeypatch.setattr(m,'Worker',lambda c:e.w);monkeypatch.setattr(member,'Worker',lambda c:e.w)
    monkeypatch.setattr(m,'Runtime',lambda *a:e.runtime);monkeypatch.setattr(e.w,'close',lambda:None)
    real_popen=subprocess.Popen
    def popen(command,*args,**kw):
        if len(command)>2 and command[1]=='-m' and command[2] in ('ops.zkspv.contour_member','ops.zkspv.contour_mint'):
            monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',kw['env']['SATOHSI_CONTOUR_CONTEXT'])
            (m if command[2].endswith('contour_mint') else member).main(command[3:])
            return NS(pid=999999999,wait=lambda **kw:0)
        return real_popen(command,*args,**kw)
    monkeypatch.setattr(subprocess,'Popen',popen)
    runner=Contour(plan)
    for _ in range(12):
        before=len(e.raws);result=runner.step();assert len(e.raws)-before<=1
        if e.view.state==1 and not e.view.absent:e.view.state=2  # External FROST finalization fixture.
        if result['status']=='DRIVERS_COMPLETED_UNQUALIFIED':break
    assert result['status']=='DRIVERS_COMPLETED_UNQUALIFIED' and not result['combined_e2e_accepted']
    assert e.view.complete and len(e.raws)==3

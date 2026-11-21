"""Real HPKE/transport/automation/journals; canonical EVM/Core/payment effects are fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS

import pytest
from eth_account import Account
from web3 import Web3
from web3.datastructures import AttributeDict

from ops.zkspv import contour_service as m
from ops.zkspv.contour import digest
from signer_agent import crypto,spv_automation,zk_service_issuer,zk_worker_service
from signer_agent.gas_treasury import GasTreasuryClient
from signer_agent.spv_transport import Client,Store,route_key,message_key,open_packet
from signer_agent.zk_worker import Inbox
from signer_agent.zk_worker_transport import WorkerTransport
from signer_agent.zk_worker_prover import write,read
from test_spv_transport import client_config
from test_contour_worker import pipeline

REAL_RUN=subprocess.run
REAL_POPEN=subprocess.Popen


@pytest.fixture(params=['CONFEDERATION','PAID'])
def service(pipeline,request,monkeypatch):
    e=pipeline;selected_mode=request.param;mode='PAID' if selected_mode=='PAID' else 'CONFEDERATION';fake_run=subprocess.run
    monkeypatch.setattr(subprocess,'run',lambda command,*args,**kw:
        REAL_RUN(command,*args,**kw) if '--ipc' in command else fake_run(command,*args,**kw))
    (e.tmp_path/'requester').mkdir(mode=0o700);(e.tmp_path/'peer').mkdir(mode=0o700)
    rc,rk=client_config(e.tmp_path/'requester');pc,pk=client_config(e.tmp_path/'peer')
    write(pc['identity_file'],dict(private_key='22'*32))
    peer_account=Account.from_key('22'*32);requester_account=Account.from_key('11'*32)
    write(e.tmp_path/'issuer.json',dict(private_key='33'*32));issuer=Account.from_key('33'*32).address
    c=e.worker.c;e.worker.inbox.close()
    c.update(version='4',directory=str(e.tmp_path/'service-worker'),bitcoin_policy=rc['bitcoin_policy'],
        service=dict(mode='paid_or_confederation' if selected_mode=='OWN_FALLBACK' else 'confederation' if mode=='CONFEDERATION' else 'paid',binding=None,
            max_self_funded_cost_wei=str(10**19),max_self_funded_job_cost_wei=str(10**18)),
        heartbeat=dict(enabled=False,interval_seconds='300',lead_seconds='30',grace_seconds='60',safety_margin_seconds='10',
            finality_budget_seconds='30',continuous_fresh=False,max_status_attempts='2',status_backoff_seconds='5'),
        batching=dict(local_service_seconds='300'),
        transport=dict(config_file=str(e.tmp_path/'peer-config.json'),
            federation_consumers={'fed':[e.request['consumer']]},status_interval_seconds='1'),
        archive=dict(directory=str(e.tmp_path/'archive'),max_headers='200000',max_bytes=str(10**9)),automation=dict(enabled=False))
    binding=dict(schema='satoshifi.spv-gas-binding.v1',chain_id=c['chain_id'],evm_confirmations=c['evm_confirmations'],
        bitcoin_policy=c['bitcoin_policy'],factory=rc['factory'],confederation_id=rc['confederation_id'],
        registry=Web3.to_checksum_address('0x'+'66'*20),domain='0x'+'dd'*32,state_view=Web3.to_checksum_address('0x'+'77'*20))
    policy=dict(max_tx_gas='100000',max_transaction_cost_wei='100',max_total_cost_wei='10000',
                max_job_cost_wei='1000',max_attempts_per_job='8')
    if selected_mode=='OWN_FALLBACK':c['service']['binding']=binding
    if mode=='PAID':
        c['service']['binding']=binding
        c['automation']=dict(enabled=True,issuer_identity_file=str(e.tmp_path/'issuer.json'),permission_seconds='300',**policy)
        rc.update(version='2',payment=dict(enabled=True,binding=binding,identity_file=rc['identity_file'],
            bitcoin_rpc=c['bitcoin_rpc'],bitcoin_cookie=c['bitcoin_cookie'],budget_scope='aa'*32,allowed_federations=['fed'],
            allowed_consumers=c['allowed_consumers'],allowed_service_identities=[peer_account.address],
            treasury=Web3.to_checksum_address('0x'+'99'*20),treasury_epoch='1',roster_hash='bb'*32,
            tariff_version='1',tariff_hash='cc'*32,max_service_fee_wei='100',**policy))
    e.worker.inbox=Inbox(c);e.worker.archive_ref=lambda state:[]
    e.clock=1500000000;e.block_hash=b'h'*32;e.packets={};e.routes={};e.messages={};e.effects=[];e.signed={}
    e.msg_crash=None;e.gas_crash=None;e.gas_action=None;e.allow_request=True;e.allow_service=True;e.gas_sends=0;e.gas_recovers=0
    e.jobs={};e.paid=set();e.permission={};e.generation=1;e.tariff=1;e.balance=10**20
    def block(n):return AttributeDict(dict(number=10 if not isinstance(n,int) else n,hash=e.block_hash,timestamp=e.clock))
    old_context=e.worker.context
    e.worker.context=lambda **kw:(block('latest'),*old_context(**kw)[1:])
    e.worker.w3.eth.get_block=block;e.worker.w3.eth.get_balance=lambda *a,**kw:e.balance
    e.worker.observe_need=lambda *args:dict(status=e.request_status,height=32250,depth=6,header='fixture-header')
    zero=Web3.to_checksum_address('0x'+'00'*20)
    blank=(bytes(32),0,0,zero,zero,0,0,0,b'')
    keys={requester_account.address:rk,peer_account.address:pk}
    call=lambda function:lambda *args:NS(call=lambda *tx,**kw:function(*args))
    board=NS(functions=NS(
        packet=call(lambda key:e.packets.get(bytes(key).hex(),blank)),
        route=call(lambda key:e.routes.get(bytes(key).hex(),(zero,zero,'',bytes(32)))),
        messageCount=call(lambda who:len(e.messages.get(who,[]))),
        messageAt=call(lambda who,i:bytes.fromhex(e.messages[who][i])),
        currentEpoch=call(lambda who:1),transportKeys=call(lambda who,epoch:bytes.fromhex(keys[who]['public'])),
        requesterAllowed=call(lambda fed,who:e.allow_request and fed=='fed' and who==requester_account.address),
        serviceAllowed=call(lambda who:e.allow_service and who==peer_account.address),
        stateView=call(lambda:binding['state_view'])))
    e.board=board
    def fake_contract(**kw):return NS(functions=NS(adapter=call(lambda:Web3.to_checksum_address('0x'+'88'*20)),
        getState=call(lambda:[bytes(32)]*12),stateVersion=call(lambda:1)))
    w3=NS(eth=NS(chain_id=31337,block_number=10,get_block=block,contract=fake_contract,
                get_transaction_receipt=lambda key:e.receipts[key]))
    requester=Client(rc,w3);peer=Client(pc,w3)
    for client in (requester,peer):client.context=lambda:(block('latest'),board,'ab'*32)
    def send(client,operation,method,args,*,recover_only=False):
        identity=(client.account.address,operation);old=e.signed.get(identity)
        if recover_only and old is None:return dict(status='NOT_SIGNED')
        if old is None:
            if e.msg_crash=='unsigned':raise RuntimeError('unsigned message interruption fixture')
            client.store.reserve(operation,10);e.signed[identity]=(method,deepcopy(args))
            if e.msg_crash=='signed':raise RuntimeError('signed message interruption fixture')
        elif old!=(method,args):raise ValueError('EXACT_MESSAGE_CHANGED')
        if method=='request':
            rid,fed,recipient,se,re,expires,cipher=args;rid=rid.removeprefix('0x')
            route=route_key(rid,client.account.address,recipient);kind=0;seq=0
            e.routes[route]=(client.account.address,recipient,fed,bytes.fromhex(rid))
        else:
            route,kind,seq,se,re,expires,cipher=args;route=route.removeprefix('0x');kind=int(kind);seq=int(seq)
            recipient=e.routes[route][0]
        key=message_key(route,{0:'REQUEST',1:'ACK',2:'STATUS'}[kind],str(seq))
        packet=(bytes.fromhex(route),kind,seq,client.account.address,recipient,int(se),int(re),int(expires),bytes.fromhex(cipher[2:]))
        if key not in e.packets:
            e.packets[key]=packet;e.messages.setdefault(recipient,[]).append(key);e.effects.append(('message',key))
        elif e.packets[key]!=packet:raise ValueError('PACKET_CHANGED')
        return dict(status='VERIFIED')
    for client in (requester,peer):client.send=lambda *a,_client=client,**kw:send(_client,*a,**kw)
    transport=WorkerTransport.__new__(WorkerTransport);transport.worker=e.worker;transport.client=peer
    e.requester,e.peer,e.transport=requester,peer,transport
    class Gas(GasTreasuryClient):
        def __init__(self,w3,manifest):self.w3=NS(eth=NS(chain_id=31337,contract=fake_contract));self.manifest=manifest;self.registry=NS(address=binding['registry'],functions=NS(admit=call(lambda *a:None),fundRequest=call(lambda *a:None)))
        def _binding(self):return block('latest')
        def _anchor(self,b):assert b.hash==e.block_hash
        def service_profile(self,b):return NS()
        def _treasury(self,*a):return NS(address=Web3.to_checksum_address('0x'+'99'*20))
        def _call(self,target,name,b,*args):
            if name=='configuration':return [Web3.to_checksum_address('0x'+'99'*20),issuer,e.tariff,bytes.fromhex('cc'*32),20,bytes(32)]
            if name=='jobs':return e.jobs.get(bytes.fromhex(args[0][2:]) if isinstance(args[0],str) else bytes(args[0]),[bytes(32)]*3+[zero,bytes(32),0])
            if name=='permissions':return e.permission.get(bytes.fromhex(args[0][2:]) if isinstance(args[0],str) else bytes(args[0]),[0,1,bytes.fromhex('aa'*32),1,e.clock+1000,False])
            if name=='semanticJob':return next((job for job,t in e.jobs.items() if t[1]==args[0]),bytes(32))
            if name=='paid':return bytes.fromhex(args[0][2:]) in e.paid if isinstance(args[0],str) else bytes(args[0]) in e.paid
            return {'state':c['zk_state'],'snapshot':[bytes.fromhex('aa'*32),1,True,False],
                'generation':e.generation,'enabled':True,'epoch':1,'rosterHash':bytes.fromhex('bb'*32),
                'payments':[20]}[name]
        def submit(self,r,identity_file,*,budget=None,recover_only=False):
            operation=r['action']+':'+digest(r);old=e.signed.get(operation)
            if recover_only:
                e.gas_recovers+=1
                if old is None:return dict(status='NOT_SIGNED')
            if old is None:
                if e.gas_crash=='unsigned' and e.gas_action in (None,r['action']):raise RuntimeError('unsigned gas interruption fixture')
                budget.reserve(operation,dict(gas=10,gasPrice=1,value=int(r['value'])));e.signed[operation]=deepcopy(r);e.gas_sends+=1
                if e.gas_crash=='signed' and e.gas_action in (None,r['action']):raise RuntimeError('signed gas interruption fixture')
            elif old!=r:raise ValueError('GAS_REQUEST_CHANGED')
            if r['action']=='admit':
                t=r['terms'];job=bytes.fromhex(t['jobId'][2:])
                e.jobs[job]=[job,bytes.fromhex(t['semanticKey'][2:]),bytes.fromhex(t['federationIdHash'][2:]),t['payer'],bytes.fromhex(t['budgetScope'][2:]),int(t['serviceDeadline'])]
                e.permission[job]=[int(r['args'][-1]),e.generation,bytes.fromhex(r['args'][1][2:]),int(r['args'][2]),int(r['accept_by']),True]
            else:e.paid.add(bytes.fromhex(r['job_id'][2:]))
            effect=(r['action'],r['job_id'] if r['action']=='fund' else r['terms']['jobId'])
            if effect not in e.effects:e.effects.append(effect)
            txid=Web3.to_hex(Web3.keccak(text=operation))
            e.receipts[txid]=AttributeDict(dict(status=1,blockNumber=10,blockHash=e.block_hash,transactionHash=bytes.fromhex(txid[2:]),gasUsed=10))
            return dict(status='CONFIRMED',transaction_hash=txid,block_number='10',block_hash=e.block_hash.hex(),gas_used='10')
    for module in (spv_automation,zk_service_issuer,zk_worker_service):monkeypatch.setattr(module,'GasTreasuryClient',Gas)
    monkeypatch.setattr('signer_agent.gas_treasury.GasTreasuryClient',Gas)
    monkeypatch.setattr('signer_agent.bitcoin_rpc.BitcoinRPC',lambda *a:NS())
    observation=lambda *a:(dict(status='NEEDS_STATE' if e.request_status=='NEEDS_STATE' else 'COVERED_BY_STATE',height=32250,depth=6),
                             dict(blocks=32257,bestblockhash=e.worker.bitcoin.call('getblockhash',32257)))
    monkeypatch.setattr(spv_automation,'observation',observation);monkeypatch.setattr(zk_service_issuer,'observation',observation)
    sc=dict(version='1',requester_config=str(e.tmp_path/'requester-config.json'),federation='fed',
            service_identity=peer_account.address,revision='0',funding_mode=mode)
    write(sc['requester_config'],rc);write(c['transport']['config_file'],pc);write(e.tmp_path/'service.json',sc)
    e.sc=sc;e.mode=mode;e.service_counter=0
    def call_phase(phase,action='probe',*,plan='bb'*32):
        e.clock+=5;e.service_counter+=1
        invocation=e.tmp_path/('service-invocation-'+str(e.service_counter));invocation.mkdir(mode=0o700)
        ctx=dict(run_id='aa'*32,plan_sha256=plan,capability=phase,stage=phase,attempt_id=digest([plan,phase]),action=action)
        p=m.Pipeline(e.worker,e.request,ctx,invocation,sc,requester,transport,config_file=e.tmp_path/'service.json')
        before=len(e.effects);result=p.execute(action);assert len(e.effects)-before<=1
        return p,result
    e.service_call=call_phase
    def deliver():
        e.service_call('consumer_request','run')
        for _ in range(12):
            p,result=e.service_call('encrypted_transport','recover')
            if result['status']=='SATISFIED':return p
            assert result['status'] in ('ABSENT','WAITING'),result
        raise AssertionError('delivery incomplete')
    e.delivered=deliver
    def funded():
        e.delivered()
        for _ in range(20):
            p,result=e.service_call('admission_payment','recover')
            if result['status']=='SATISFIED':return p,result
            assert result['status'] in ('ABSENT','WAITING'),result
        raise AssertionError('funding incomplete')
    e.funded=funded
    yield e
    requester.close();peer.close()


def test_encrypted_request_ack_status_and_funding_use_real_clients_and_durable_ids(service):
    e=service;p,result=e.funded();assert result['status']=='SATISFIED'
    assert len(e.worker.inbox.needs())==1
    original=p.original();assert e.request['txid'] not in original['ciphertext']
    assert open_packet('ab'*32,original['header'],original['ciphertext'],e.peer.keys[1]['secret'])['need']==e.request
    before=list(e.effects)
    assert e.service_call('admission_payment')[1]['status']=='SATISFIED'
    assert e.effects==before
    source=result['observations']['funding']['source']
    assert source==('PAID_SERVICE' if e.mode=='PAID' else 'CONFEDERATION')
    assert len(e.paid)==(1 if e.mode=='PAID' else 0)


def test_request_retry_preserves_ciphertext_nonce_deadline_and_messages(service):
    e=service;p=e.delivered();original=deepcopy(p.original());deadline=e.worker.inbox.get('obligation:'+p.need_id())['deadline']
    assert e.service_call('consumer_request','recover')[1]['status']=='SATISFIED'
    q,result=e.service_call('encrypted_transport','recover')
    assert result['status']=='SATISFIED' and q.original()==original
    assert e.worker.inbox.get('obligation:'+p.need_id())['deadline']==deadline
    assert len(e.effects)==3


def test_ack_is_only_delivery_and_cannot_mark_paid(service):
    e=service;e.delivered()
    assert not e.paid and e.gas_sends==0
    if e.mode=='PAID':assert e.service_call('admission_payment')[1]['status']=='ABSENT'


def test_own_funding_does_not_consult_or_manufacture_treasury_payment(service,monkeypatch):
    e=service
    if e.mode!='CONFEDERATION':
        changed=dict(e.sc,funding_mode='CONFEDERATION')
        with pytest.raises(ValueError,match='FUNDING_POLICY'):m.validate(e.worker.c,changed,e.requester.c,e.peer.c,e.request)
        return
    class Forbidden:
        def __init__(self,*a):pytest.fail('own funding must not depend on project treasury')
    monkeypatch.setattr(zk_worker_service,'GasTreasuryClient',Forbidden)
    monkeypatch.setattr(zk_service_issuer,'GasTreasuryClient',Forbidden)
    p,result=e.funded()
    assert result['observations']['funding']==dict(source='CONFEDERATION',jobs=[],paid_service=False)
    assert not e.paid and e.gas_sends==0
    assert not e.worker.inbox.get('service:'+p.need_id())


def test_final_encrypted_status_never_claims_monetary_readiness(service):
    e=service;e.funded()
    for _ in range(5):
        p,result=e.service_call('service_status','recover')
        if result['status']=='SATISFIED':break
    assert result['status']=='SATISFIED' and result['observations']['reported_status']=='NEEDS_STATE'
    assert result['observations']['ready'] is False


def test_unknown_or_unsigned_message_recovery_does_not_allocate_another_envelope(service):
    e=service;e.service_call('consumer_request','run');e.msg_crash='unsigned'
    with pytest.raises(RuntimeError,match='unsigned message'):e.service_call('encrypted_transport','run')
    e.msg_crash=None;e.clock+=120
    assert e.service_call('encrypted_transport','recover')[1]['status']=='RECOVERY_REQUIRED'
    assert not e.signed and not e.packets


def test_signed_message_recovery_reuses_original_ciphertext_and_envelope(service):
    e=service;p,_=e.service_call('consumer_request','run');original=deepcopy(p.original());e.msg_crash='signed'
    with pytest.raises(RuntimeError,match='signed message'):e.service_call('encrypted_transport','run')
    e.msg_crash=None;e.clock+=120;p=e.delivered()
    assert p.original()==original and len(e.effects)==3
    assert len(e.signed)==3


def test_restart_and_another_contour_do_not_repeat_messages_or_service_payment(service):
    e=service;p,_=e.funded();original=deepcopy(p.original());effects=list(e.effects)
    for client in (e.requester,e.peer):client.store.close();client.store=Store(client.c,client.account.address)
    e.worker.inbox.close();e.worker.inbox=Inbox(e.worker.c)
    assert e.service_call('encrypted_transport')[1]['status']=='SATISFIED'
    assert e.service_call('admission_payment')[1]['status']=='SATISFIED'
    e.service_call('consumer_request','run',plan='cc'*32)
    q,result=e.service_call('encrypted_transport','recover',plan='cc'*32)
    assert result['status']=='SATISFIED' and q.original()==original
    assert e.service_call('admission_payment',plan='cc'*32)[1]['status']=='SATISFIED'
    assert e.effects==effects


def test_orphaned_ack_blocks_new_funding_without_erasing_obligations(service):
    e=service;p=e.delivered();key=p.record['ack'];del e.packets[key]
    for who in e.messages:e.messages[who]=[x for x in e.messages[who] if x!=key]
    e.block_hash=b'z'*32
    assert e.service_call('admission_payment')[1]['status']=='RECOVERY_REQUIRED'
    assert e.worker.inbox.get('remote:'+p.record['route'])['local_need_id']==p.need_id()
    assert e.gas_sends==0 and not e.paid


def test_canonical_packet_conflict_is_not_a_delivery_receipt(service):
    e=service;p=e.delivered();key=p.record['ack'];packet=list(e.packets[key]);packet[8]=b'changed encrypted packet';e.packets[key]=tuple(packet)
    with pytest.raises(ValueError,match='PACKET_CONFLICT|DECRYPTED_CONFLICT'):e.service_call('admission_payment')
    assert e.gas_sends==0


def test_halted_status_can_be_delivered_without_proving_or_new_payment(service):
    e=service;e.funded();old=e.worker.context
    e.worker.context=lambda **kw:(*old(**kw)[:4],None)
    for _ in range(5):
        _,result=e.service_call('service_status','recover')
        if result['status']=='SATISFIED':break
    assert result['status']=='SATISFIED' and result['observations']['reported_status']=='HALTED'
    assert e.stages==[] and not result['observations']['ready']


@pytest.mark.parametrize('service',['CONFEDERATION'],indirect=True)
def test_own_fee_reservations_survive_and_bound_new_work(service):
    e=service;e.worker.c['service']['max_self_funded_cost_wei']='10';e.worker.c['service']['max_self_funded_job_cost_wei']='10'
    p=e.delivered();zk_worker_service.reserve(e.worker,'previous-native-gas',10,[p.need_id()])
    with pytest.raises(ValueError,match='SELF_FUNDING_BUDGET'):e.service_call('admission_payment')
    assert dict(e.worker.inbox.db.execute('SELECT * FROM reservations'))=={'previous-native-gas':'10'}
    assert not e.paid and e.gas_sends==0


@pytest.mark.parametrize('service',['PAID'],indirect=True)
def test_unsigned_admission_crash_does_not_renew_permission_or_allocate_again(service):
    e=service;e.delivered();e.gas_crash='unsigned'
    with pytest.raises(RuntimeError,match='unsigned gas'):e.service_call('admission_payment','run')
    original=deepcopy(e.worker.inbox.get('auto_admission'));e.gas_crash=None
    assert e.service_call('admission_payment','recover')[1]['status']=='RECOVERY_REQUIRED'
    assert e.worker.inbox.get('auto_admission')==original and e.gas_sends==0 and not e.paid


@pytest.mark.parametrize('service',['PAID'],indirect=True)
def test_signed_admission_recovery_uses_original_request(service):
    e=service;e.delivered();e.gas_crash='signed'
    with pytest.raises(RuntimeError,match='signed gas'):e.service_call('admission_payment','run')
    original=deepcopy(e.worker.inbox.get('auto_admission')['request']);e.gas_crash=None
    e.service_call('admission_payment','recover')
    assert e.gas_sends==1 and e.gas_recovers==1
    assert e.signed['admit:'+digest(original)]==original


@pytest.mark.parametrize('service',['PAID'],indirect=True)
def test_signed_fund_recovery_precedes_covered_state_and_retains_paid_fact(service,monkeypatch):
    e=service;e.delivered();e.gas_crash='signed';e.gas_action='fund'
    with pytest.raises(RuntimeError,match='signed gas'):
        for _ in range(24):e.service_call('admission_payment','run')
    saved={k:v for k,v in e.signed.items() if isinstance(k,str) and k.startswith('fund:')}
    assert len(saved)==1;e.gas_crash=None;e.clock+=120;e.request_status='READY'
    monkeypatch.setattr(spv_automation,'observation',lambda *_:pytest.fail('recover signed funding before a new Core observation'))
    e.service_call('admission_payment','recover')
    _,result=e.service_call('admission_payment')
    assert result['status']=='SATISFIED' and result['observations']['funding']['paid_service']
    assert len(e.paid)==1 and {k:v for k,v in e.signed.items() if isinstance(k,str) and k.startswith('fund:')}==saved


@pytest.mark.parametrize('service',['PAID'],indirect=True)
def test_unsigned_fund_crash_cannot_create_another_value_transfer(service):
    e=service;e.delivered();e.gas_crash='unsigned';e.gas_action='fund'
    with pytest.raises(RuntimeError,match='unsigned gas'):
        for _ in range(24):e.service_call('admission_payment','run')
    e.gas_crash=None;e.clock+=120
    assert e.service_call('admission_payment','recover')[1]['status']=='RECOVERY_REQUIRED'
    assert e.gas_sends==1 and not e.paid


@pytest.mark.parametrize('service',['PAID'],indirect=True)
def test_above_consent_fee_never_creates_admission_or_payment(service):
    e=service;e.requester.c['payment']['max_service_fee_wei']='19';e.delivered()
    for _ in range(4):assert e.service_call('admission_payment','run')[1]['status']!='SATISFIED'
    assert e.gas_sends==0 and not e.paid


def test_funded_service_prerequisite_is_rechecked_before_native_preparation(service,monkeypatch):
    e=service;e.funded()
    monkeypatch.setattr(m,'Client',lambda c:e.requester)
    monkeypatch.setattr(m,'WorkerTransport',lambda worker:e.transport)
    # Guard closes temporary readers; fixture actors are retained for subsequent checks.
    monkeypatch.setattr(e.requester,'close',lambda:None);monkeypatch.setattr(e.transport,'close',lambda:None)
    p,result=e.call('proof_preparation','run')
    assert result['status']=='SATISFIED' and e.stages==[] and e.publishes==0
    if e.mode=='PAID':e.paid.clear()
    else:e.balance=0
    with pytest.raises(ValueError,match='CONTOUR_SERVICE_FUNDING_REQUIRED|ZK_PUBLISHER_BALANCE_REQUIRED'):
        p.service_guard()


def test_offline_plan_wires_service_phases_and_pins_all_actor_configs(service):
    from ops.zkspv.contour_plan import build
    e=service;wc=e.tmp_path/'worker.json';need=e.tmp_path/'need.json';write(wc,e.worker.c);write(need,e.request)
    plan=build(wc,need,e.tmp_path/'controller',worker_only=True,service_config=e.tmp_path/'service.json')
    assert [s['capability'] for s in plan['stages']]==['consumer_request','encrypted_transport','admission_payment',
        'proof_preparation','fresh_proof','state_update','service_status']
    commands={s['capability']:s['run'] for s in plan['stages']}
    assert all('ops.zkspv.contour_service' in commands[k] for k in m.PHASES)
    assert 'ops.zkspv.contour_worker' in commands['fresh_proof']
    assert digest(e.sc) in commands['admission_payment'] and digest(e.requester.c) in commands['encrypted_transport']


@pytest.mark.parametrize('service',['OWN_FALLBACK'],indirect=True)
def test_paid_or_confederation_policy_can_explicitly_use_own_funds_without_a_treasury(service,monkeypatch):
    e=service;assert e.worker.c['service']['mode']=='paid_or_confederation'
    class Forbidden:
        def __init__(self,*a):pytest.fail('no treasury lookup for explicit own-funded work')
    monkeypatch.setattr(zk_worker_service,'GasTreasuryClient',Forbidden)
    _,result=e.funded()
    assert result['observations']['funding']['source']=='CONFEDERATION' and not e.paid and e.gas_sends==0


@pytest.mark.parametrize('service',['PAID'],indirect=True)
def test_compatible_alias_reuses_the_original_paid_job_without_a_second_quote_or_payment(service):
    e=service;p,first=e.funded();job=first['observations']['funding']['jobs'][0]
    deadline=e.worker.inbox.get('auto_coverage')['terms']['serviceDeadline'];gas=e.gas_sends
    e.request=dict(e.request,vout='1')
    e.service_call('consumer_request','run',plan='cc'*32)
    for _ in range(10):
        q,result=e.service_call('encrypted_transport','recover',plan='cc'*32)
        if result['status']=='SATISFIED':break
    assert result['status']=='SATISFIED'
    for _ in range(10):
        q,result=e.service_call('admission_payment','recover',plan='cc'*32)
        if result['status']=='SATISFIED':break
    assert result['status']=='SATISFIED' and result['observations']['funding']['jobs']==[job]
    assert e.gas_sends==gas and len(e.paid)==1
    assert e.worker.inbox.get('auto_coverage')['terms']['serviceDeadline']==deadline
    assert not any(r['body'].get('status')=='QUOTE' for _,r,_ in e.requester.store.incoming('STATUS') if r['header']['route']==q.record['route'])


def test_scoped_ingest_does_not_admit_an_unselected_request(service):
    e=service;e.service_call('consumer_request','run');e.service_call('encrypted_transport','run')
    e.transport.ingest(routes=[])
    assert e.worker.inbox.needs()==[] and e.gas_sends==0


@pytest.mark.parametrize('service',['PAID'],indirect=True)
def test_scoped_admission_does_not_submit_for_another_route(service):
    e=service;e.delivered()
    assert zk_service_issuer.tick(e.worker,e.transport,routes=[])['status']=='IDLE'
    assert e.gas_sends==0 and e.worker.inbox.get('auto_coverage') is None


def test_funding_mode_or_peer_identity_cannot_be_relabeled_in_a_saved_plan(service):
    e=service;e.delivered();original=e.sc['service_identity'];e.sc['service_identity']=e.requester.account.address
    with pytest.raises(ValueError,match='CONTOUR_SERVICE_IDENTITY'):e.service_call('admission_payment')
    e.sc['service_identity']=original;e.sc['revision']='1'
    with pytest.raises(ValueError,match='CONTOUR_SERVICE_SCOPE'):e.service_call('consumer_request','recover')


def test_runner_cli_chain_connects_encrypted_service_and_worker_phases(service,monkeypatch):
    from ops.zkspv.contour import Contour
    from ops.zkspv.contour_plan import build
    from ops.zkspv import contour_worker
    e=service;wc=e.tmp_path/'worker.json';need=e.tmp_path/'need.json';write(wc,e.worker.c);write(need,e.request)
    plan=build(wc,need,e.tmp_path/'cli-controller',worker_only=True,service_config=e.tmp_path/'service.json')
    monkeypatch.setattr(m,'Worker',lambda c:e.worker);monkeypatch.setattr(contour_worker,'Worker',lambda c:e.worker)
    monkeypatch.setattr(m,'Client',lambda c:e.requester);monkeypatch.setattr(m,'WorkerTransport',lambda w:e.transport)
    monkeypatch.setattr(e.worker,'close',lambda:None);monkeypatch.setattr(e.requester,'close',lambda:None)
    monkeypatch.setattr(e.transport,'close',lambda:None)
    def popen(command,*args,**kw):
        if len(command)>2 and command[1]=='-m' and command[2] in ('ops.zkspv.contour_service','ops.zkspv.contour_worker'):
            monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',kw['env']['SATOHSI_CONTOUR_CONTEXT'])
            (m if command[2].endswith('contour_service') else contour_worker).main(command[3:])
            return NS(pid=999999999,wait=lambda **kw:0)
        return REAL_POPEN(command,*args,**kw)  # Real native HPKE child processes remain real.
    monkeypatch.setattr(subprocess,'Popen',popen)
    runner=Contour(plan)
    for _ in range(60):
        e.clock+=5;effects=len(e.effects);result=runner.step()
        assert len(e.effects)-effects<=1
        if result['status']=='DRIVERS_COMPLETED_UNQUALIFIED':break
    assert result['status']=='DRIVERS_COMPLETED_UNQUALIFIED'
    assert not result['production_ready'] and not result['combined_e2e_accepted']
    assert 'address_mint' in result['missing_capabilities'] and e.publishes==1
    assert e.stages==['composite','succinct','groth16'] and len(e.paid)==(1 if e.mode=='PAID' else 0)

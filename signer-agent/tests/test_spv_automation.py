"""Consent/cost invariants and canonical quote automation with explicit RPC fakes."""
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from signer_agent.spv_automation import Budget,offer,validate_payment
from signer_agent.zk_worker_prover import write
from test_spv_transport import fake_client,client_config


def budget_policy():
    return dict(max_tx_gas='100000',max_transaction_cost_wei='100',max_total_cost_wei='150',max_job_cost_wei='100',max_attempts_per_job='2')


def test_budget_is_per_identity_durable_and_reservations_do_not_reset(tmp_path):
    ident=tmp_path/'payer.json';write(ident,dict(private_key='11'*32))
    p=budget_policy();b=Budget(ident,'registry:payer',p,'job1')
    tx=dict(gas=10,gasPrice=2,value=20)
    b.reserve('attempt1',tx);b.reserve('attempt1',tx)
    b=Budget(ident,'registry:payer',p,'job1');b.reserve('attempt2',tx)
    with pytest.raises(ValueError,match='ATTEMPT_BUDGET'):b.reserve('attempt3',tx)
    other=Budget(ident,'registry:payer',p,'job2');other.reserve('other',tx)
    with pytest.raises(ValueError,match='TOTAL_BUDGET'):other.reserve('more',tx)
    changed=dict(p,max_total_cost_wei='300')
    with pytest.raises(ValueError):Budget(ident,'registry:payer',changed,'job3')


def payment_policy(c):
    return dict(enabled=True,binding={k:c[k] for k in ('chain_id','evm_confirmations','bitcoin_policy','factory','confederation_id')},
        identity_file=c['identity_file'],bitcoin_rpc='http://127.0.0.1:1',bitcoin_cookie=str(Path(c['directory']).parent/'cookie'),
        budget_scope='aa'*32,allowed_federations=['fed'],allowed_consumers={'0x'+'88'*20:'MiningPoolV1'},
        allowed_service_identities=[__import__('eth_account',fromlist=['Account']).Account.from_key('22'*32).address],
        treasury='0x'+'99'*20,treasury_epoch='1',roster_hash='bb'*32,tariff_version='1',tariff_hash='cc'*32,
        max_service_fee_wei='100',**budget_policy())


def test_offer_requires_private_opt_in_and_matches_exact_identity_scope(tmp_path):
    client,recipient,_,_,_=fake_client(tmp_path)
    need=dict(kind='deposit',consumer='0x'+'88'*20,txid='bb'*32,vout='0')
    assert offer(client,need,'fed',recipient) is None
    p=payment_policy(client.c);validate_payment(client.c,p);client.c['payment']=p
    accepted=offer(client,need,'fed',recipient)
    assert accepted['payer']==client.account.address and accepted['max_service_fee_wei']=='100'
    with pytest.raises(ValueError,match='PAYMENT_SCOPE'):offer(client,need,'other',recipient)
    with pytest.raises(ValueError,match='PAYMENT_SCOPE'):offer(client,dict(need,consumer='0x'+'77'*20),'fed',recipient)
    client.close()


def payer_fixture(tmp_path,monkeypatch,covered=False,kind='deposit'):
    import hashlib,json
    import signer_agent.spv_automation as module
    from signer_agent.spv_transport import message_key
    client,recipient,_,block,board=fake_client(tmp_path)
    p=payment_policy(client.c);p['binding'].update(schema='satoshifi.spv-gas-binding.v1',registry='0x'+'66'*20,domain='0x'+'dd'*32)
    client.c['payment']=p
    need=dict(kind=kind,consumer='0x'+'88'*20,txid='bb'*32,vout='0')
    if kind=='address_deposit':
        need['intent_id']='ab'*32
        client.c['address_deposits']=dict(version='1',registries={need['consumer']:'0x'+'44'*20})
    _,original=client.send_request(need,'fed',recipient)
    route=original['header']['route'];job='0x'+'ee'*32
    fund=dict(schema='satoshifi.spv-gas-request.v1',action='fund',chain_id='31337',registry='0x'+'66'*20,domain='0x'+'dd'*32,
        job_id=job,payer=client.account.address,
        terms=[job,'0x'+'ff'*32,'0x'+__import__('web3',fromlist=['Web3']).Web3.keccak(text='fed').hex(),client.account.address,'0x'+p['budget_scope'],original['body']['service_deadline']],
        configuration=[p['treasury'],'issuer',p['tariff_version'],'0x'+p['tariff_hash'],'20','0x'+'00'*32],
        permission=['1','1','0x'+'aa'*32,'1','9999',True],treasury_epoch=p['treasury_epoch'],roster_hash='0x'+p['roster_hash'],value='20')
    cipher=b'canonical encrypted packet'
    ack_key=message_key(route,'ACK','0');quote_key=message_key(route,'STATUS','1')
    ack=dict(header=dict(original['header'],kind='ACK',sender=recipient),body=dict(status='ACCEPTED',semantic_key='ff'*32),ciphertext_sha256=hashlib.sha256(cipher).hexdigest())
    body=dict(version='1',status='QUOTE',request_id=original['request_id'],operation_id=original['body']['operation_id'],quote=dict(schema='satoshifi.spv-service-quote.v1',request=fund,coverage={}))
    quote_record=dict(header=dict(original['header'],kind='STATUS',sender=recipient),body=body,ciphertext_sha256=hashlib.sha256(cipher).hexdigest())
    for k,r in [(ack_key,ack),(quote_key,quote_record)]:client.store.db.execute("INSERT INTO incoming VALUES(?,?,'HANDLED')",(k,json.dumps(r)))
    board.functions.packet=lambda k:NS(call=lambda **kw:[None]*8+[cipher])
    state=dict(paid=False,sends=0,recoveries=0)
    class Gas:
        def __init__(self,w3,binding):self.manifest=binding;self.registry=NS(address=binding['registry'])
        def status(self,job):return dict(paid=state['paid'])
        def prepare_fund(self,job):return fund
        def submit(self,r,identity_file,*,budget=None,recover_only=False):
            if recover_only:state['recoveries']+=1;return dict(status='CONFIRMED')
            budget.reserve('fund-op',dict(gas=10,gasPrice=2,value=int(r['value'])))
            state['sends']+=1;state['paid']=True;return dict(status='CONFIRMED')
    monkeypatch.setattr(module,'GasTreasuryClient',Gas)
    monkeypatch.setattr(module,'observation',lambda *a:(dict(status='COVERED_BY_STATE' if covered else 'NEEDS_STATE'),{}))
    monkeypatch.setattr('signer_agent.bitcoin_rpc.BitcoinRPC',lambda *a:NS())
    return client,state,quote_key,fund


def test_automatic_payer_pays_once_after_canonical_quote_and_local_consent(tmp_path,monkeypatch):
    from signer_agent.spv_automation import payer_tick
    c,state,key,request=payer_fixture(tmp_path,monkeypatch)
    assert payer_tick(c)['status']=='CONFIRMED' and state['sends']==1
    assert payer_tick(c)['status']=='IDLE' and state['sends']==1
    assert c.store.get('payment:'+key)['status']=='CONFIRMED';c.close()


def test_covered_root_skips_payment(tmp_path,monkeypatch):
    from signer_agent.spv_automation import payer_tick
    c,state,key,_=payer_fixture(tmp_path,monkeypatch,covered=True)
    assert payer_tick(c)['status']=='IDLE' and state['sends']==0
    assert c.store.get('payment:'+key)['status']=='COVERED';c.close()


def test_quote_above_explicit_cap_cannot_debit(tmp_path,monkeypatch):
    from signer_agent.spv_automation import payer_tick
    c,state,_,request=payer_fixture(tmp_path,monkeypatch);request['value']='101'
    # The stored ciphertext's decoded record must also carry this canonical quote.
    import json
    row=c.store.db.execute("SELECT id,body FROM incoming WHERE json_extract(body,'$.body.status')='QUOTE'").fetchone();record=json.loads(row[1]);record['body']['quote']['request']['value']='101'
    c.store.db.execute('UPDATE incoming SET body=? WHERE id=?',(json.dumps(record),row[0]))
    with pytest.raises(ValueError,match='OUTSIDE_CONSENT'):payer_tick(c)
    assert state['sends']==0;c.close()


def test_signed_payment_recovery_precedes_new_need_observation(tmp_path,monkeypatch):
    import signer_agent.spv_automation as module
    c,state,key,request=payer_fixture(tmp_path,monkeypatch)
    c.store.put('payment:'+key,dict(status='SENDING',job=request['job_id'],request=request))
    monkeypatch.setattr(module,'observation',lambda *a:pytest.fail('must recover exact signed operation first'))
    assert module.payer_tick(c)['status']=='CONFIRMED'
    assert state['recoveries']==1 and state['sends']==0;c.close()


@pytest.mark.parametrize('kind',['deposit','address_deposit'])
def test_issuer_coalesces_shared_work_without_second_full_quote(tmp_path,monkeypatch,kind):
    import json
    import signer_agent.zk_service_issuer as module
    from signer_agent.zk_worker import Inbox
    from test_zk_worker import config
    c= config(tmp_path);c['service']=dict(mode='paid',binding=dict(domain='0x'+'dd'*32))
    c['automation']=dict(enabled=True,issuer_identity_file=str(tmp_path/'issuer.json'),permission_seconds='300',**budget_policy())
    write(tmp_path/'issuer.json',dict(private_key='11'*32))
    from eth_account import Account
    issuer=Account.from_key('11'*32).address
    w=NS(c=c,inbox=Inbox(c),w3=NS(),bitcoin=NS(call=lambda *a:'tip'))
    a=w.inbox.enqueue(dict(kind=kind,consumer='a',txid='aa'*32,vout='0'))
    b=w.inbox.enqueue(dict(kind=kind,consumer='b',txid='bb'*32,vout='0'))
    for key in (a,b):w.inbox.status(key,'NEEDS_STATE')
    offered=dict(payer=issuer,budget_scope='aa'*32,treasury='treasury',treasury_epoch='1',roster_hash='bb'*32,tariff_version='1',tariff_hash='cc'*32,max_service_fee_wei='100')
    for route,key in [('route1',a),('route2',b)]:
        w.inbox.put('remote:'+route,dict(local_need_id=key,deadline=1000,request_id='id',operation_id=key,federation='fed',payment_request=offered))
    state=dict(terms=None,permission=False,paid=False,submits=0,quotes=0)
    class Gas:
        def __init__(self,*a):self.manifest=dict(domain='0x'+'dd'*32,state_view='view');self.registry=object();self.w3=NS(eth=NS(contract=lambda **kw:object()))
        def _binding(self):return NS(number=1,timestamp=100)
        def service_profile(self,b):pass
        def _treasury(self,*a):return object()
        def _anchor(self,b):pass
        def _call(self,target,name,block,*args):
            if name=='jobs' and state['terms']:
                t=state['terms']
                return [bytes.fromhex(t['jobId'][2:]),bytes.fromhex(t['semanticKey'][2:]),bytes.fromhex(t['federationIdHash'][2:]),t['payer'],bytes.fromhex(t['budgetScope'][2:]),int(t['serviceDeadline'])]
            return {'configuration':['treasury',issuer,1,bytes.fromhex('cc'*32),20,None], 'epoch':1,'rosterHash':bytes.fromhex('bb'*32),
                'semanticJob':bytes(32),'jobs':[bytes(32)]*6,'paid':state['paid'],
                'permissions':[1,1,bytes.fromhex('aa'*32),1,900,state['permission']],
                'snapshot':[bytes.fromhex('aa'*32),1,True,False],'generation':1}[name]
        def prepare_admit(self,terms,accept_by):state['terms']=terms;return dict(terms=terms)
        def submit(self,request,identity_file,*,budget=None,recover_only=False):
            if recover_only:return dict(status='CONFIRMED')
            budget.reserve('admit',dict(gas=10,gasPrice=1,value=0))
            state['permission']=True;state['submits']+=1;return dict(status='PENDING')
        def status(self,*a):return dict(paid=state['paid'])
        def prepare_fund(self,job):return dict(job_id=job)
    monkeypatch.setattr(module,'GasTreasuryClient',Gas)
    monkeypatch.setattr(module,'observation',lambda *a:(dict(status='NEEDS_STATE',height=100,depth=6),dict(blocks=110,bestblockhash='tip')))
    monkeypatch.setattr('signer_agent.spv_automation.observation',lambda *a:(dict(status='NEEDS_STATE',height=100,depth=6),dict(blocks=110,bestblockhash='tip')))
    def attach(worker,request):worker.inbox.put('service:'+a,dict(service_job_id=request['service_job_id'],need_ids=[a],deadline='1000'))
    monkeypatch.setattr(module,'attach',attach)
    class Store:
        def __init__(self):self.values={}
        def get(self,k):return self.values.get(k)
        def put(self,k,v):self.values[k]=v
    def queue(*args,**kwargs):state['quotes']+=1
    transport=NS(client=NS(store=Store(),queue_reply=queue))
    assert module.tick(w,transport)['status']=='PENDING'
    assert state['submits']==1
    module.tick(w,transport)
    coverage=w.inbox.get('auto_coverage')
    assert sorted(coverage['need_ids'])==sorted([a,b]) and state['quotes']==1
    job=coverage['job'];deadline=coverage['terms']['serviceDeadline']
    state['paid']=True;module.tick(w,transport)
    assert w.inbox.get('service:'+a)['service_job_id']==w.inbox.get('service:'+b)['service_job_id']==job
    assert w.inbox.get('auto_coverage')['terms']['serviceDeadline']==deadline and state['submits']==1
    # A completed primary never makes its still waiting alias pay again.
    w.inbox.status(a,'COMPLETED');w.inbox.status(b,'READY');module.tick(w,transport)
    w.inbox.status(b,'NEEDS_STATE');module.tick(w,transport)
    assert w.inbox.get('auto_coverage')['job']==job and state['submits']==1 and state['quotes']==1
    w.inbox.close()


@pytest.mark.parametrize('next_status',['COMPLETED','COVERED_BY_STATE','GENERATION_CHANGED'])
def test_address_payment_revalidates_at_unsigned_transaction_budget_boundary(tmp_path,monkeypatch,next_status):
    from signer_agent import spv_automation as module
    c,state,key,_=payer_fixture(tmp_path,monkeypatch,kind='address_deposit')
    calls=[]
    def observation(*_):
        calls.append(1)
        if len(calls)==1:return dict(status='NEEDS_STATE'),{}
        if next_status=='GENERATION_CHANGED':raise ValueError('BTC_MINT_GENERATION_CHANGED')
        return dict(status=next_status),{}
    monkeypatch.setattr(module,'observation',observation)
    with pytest.raises(ValueError,match='SPV_ADDRESS_WORK_NOT_REQUIRED|BTC_MINT_GENERATION_CHANGED'):module.payer_tick(c)
    assert state['sends']==0 and c.store.get('payment:'+key)['status']=='SENDING'
    c.close()


def test_address_exact_signed_payment_recovery_precedes_current_permission(tmp_path,monkeypatch):
    from signer_agent import spv_automation as module
    c,state,key,request=payer_fixture(tmp_path,monkeypatch,kind='address_deposit')
    c.store.put('payment:'+key,dict(status='SENDING',job=request['job_id'],request=request))
    monkeypatch.setattr(module,'observation',lambda *_:pytest.fail('recover signed bytes before current fences'))
    assert module.payer_tick(c)['status']=='CONFIRMED' and state['sends']==0 and state['recoveries']==1
    c.close()

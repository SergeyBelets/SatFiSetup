"""Fast transport/queue checks. Chain/provenance mocks are explicit here."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from pathlib import Path
import pytest
from eth_account import Account
from web3 import Web3
from signer_agent import crypto
from signer_agent.spv_transport import (seal,open_packet,route_key,message_key,Store,Client,load_config)
from signer_agent.zk_worker_prover import write
from signer_agent.zk_worker_batching import register,select,attempt
from signer_agent.zk_worker import Inbox
from test_zk_worker import config as worker_config


def header():
    sender=Account.from_key('11'*32).address;recipient=Account.from_key('22'*32).address
    return dict(route=route_key('aa'*32,sender,recipient),kind='REQUEST',sequence='0',sender=sender,recipient=recipient,
                sender_epoch='1',recipient_epoch='2',expires_at='1000')


def test_real_hpke_and_every_routing_field_is_authenticated():
    key=crypto.call('transport_key');h=header();body=dict(need='private transaction',deadline='999')
    ciphertext=seal('ab'*32,h,body,key['public'])
    assert open_packet('ab'*32,h,ciphertext,key['secret'])==body
    assert b'private transaction' not in bytes.fromhex(ciphertext)
    for field in h:
        other=dict(h);other[field]=h[field]+'x'
        with pytest.raises((ValueError,RuntimeError)):open_packet('ab'*32,other,ciphertext,key['secret'])
    with pytest.raises((ValueError,RuntimeError)):open_packet('cd'*32,h,ciphertext,key['secret'])
    other_key=crypto.call('transport_key')
    with pytest.raises((ValueError,RuntimeError)):open_packet('ab'*32,h,ciphertext,other_key['secret'])


def client_config(tmp_path):
    c=json.loads(Path('config/spv-transport.example.json').read_text())
    wc=worker_config(tmp_path)
    c.update(chain_id='31337',evm_confirmations='1',evm_rpc='http://127.0.0.1:1',bitcoin_policy=wc['bitcoin_policy'],
        zk_state=wc['zk_state'],factory=Web3.to_checksum_address('0x'+'33'*20),
        confederation_id='test',board=Web3.to_checksum_address('0x'+'44'*20),
        identity_file=str(tmp_path/'identity.json'),key_files=[str(tmp_path/'key.json')],directory=str(tmp_path/'transport'))
    # Match the enrolled confederation hash; the test does not authenticate fake chain objects.
    c['bitcoin_policy']['zk_spv']['confederation_id']=Web3.keccak(text='test').hex()
    write(Path(c['identity_file']),dict(private_key='11'*32))
    key=crypto.call('transport_key');write(Path(c['key_files'][0]),dict(epoch='1',**key))
    return c,key


def test_transport_private_config_scope_limits_and_durable_outbox(tmp_path):
    c,_=client_config(tmp_path);p=tmp_path/'config.json';write(p,c)
    assert load_config(p)==c
    c['max_pending_messages']='1'
    s=Store(c,header()['sender']);packet=dict(header=header(),ciphertext='aa'*80,body=dict(private='body'))
    key=s.queue('same',packet);assert s.queue('same',dict(packet,ciphertext='bb'*80))==key
    with pytest.raises(ValueError,match='OUTBOX_FULL'):s.queue('new',packet)
    s.reserve('a',8);s.reserve('a',8);s.close()
    s=Store(c,header()['sender']);assert s.outgoing('same')[1]==packet
    c2=dict(c,board=Web3.to_checksum_address('0x'+'55'*20))
    with pytest.raises(ValueError,match='SCOPE_CHANGED'):Store(c2,header()['sender'])
    s.close()


def fake_client(tmp_path):
    c,key=client_config(tmp_path);client=Client(c,w3=NS())
    recipient=Account.from_key('22'*32).address;peer=crypto.call('transport_key')
    view_address=Web3.to_checksum_address('0x'+'66'*20);adapter=Web3.to_checksum_address('0x'+'77'*20)
    def method(value):return lambda *a:NS(call=lambda **kw:value)
    board=NS(functions=NS(requesterAllowed=method(True),serviceAllowed=method(True),stateView=method(view_address),
        currentEpoch=method(1),transportKeys=lambda addr,epoch:NS(call=lambda **kw:bytes.fromhex(key['public'] if addr==client.account.address else peer['public']))))
    block=NS(number=1,timestamp=100,hash=bytes(32))
    client.w3=NS(eth=NS(contract=lambda **kw:NS(functions=NS(adapter=method(adapter),getState=method([bytes(32)]*12),stateVersion=method(0))),get_block=lambda n:block))
    client.context=lambda:(block,board,'ab'*32)
    return client,recipient,peer,block,board


def test_request_ciphertext_is_identical_on_retry_and_plaintext_is_not_sent(tmp_path):
    c,recipient,peer,block,board=fake_client(tmp_path)
    need=dict(kind='deposit',consumer=Web3.to_checksum_address('0x'+'88'*20),txid='bb'*32,vout='1')
    key,packet=c.send_request(need,'fed',recipient)
    key2,again=c.send_request(need,'fed',recipient)
    assert (key,packet)==(key2,again)
    decoded=open_packet('ab'*32,packet['header'],packet['ciphertext'],peer['secret'])
    assert decoded['need']==need and decoded['adapter']==Web3.to_checksum_address('0x'+'77'*20)
    assert decoded['service_deadline']==str(block.timestamp+int(c.c['service_sla_seconds']))
    board.functions.packet=lambda k:NS(call=lambda **kw:[bytes(32),0,0,c.account.address,recipient,1,1,0,b''])
    calls=[];c.send=lambda op,method,args:calls.append((method,args)) or dict(status='PENDING')
    assert c.transmit()['status']=='PENDING'
    method,args=calls[0]
    assert method=='request' and args[-1]=='0x'+packet['ciphertext']
    assert need['txid'] not in json.dumps(args)
    c.close()


def test_merge_preserves_deadline_paid_jobs_and_attempt_identity(tmp_path):
    cfg=worker_config(tmp_path);cfg['batching']=dict(local_service_seconds='300')
    w=NS(c=cfg,inbox=Inbox(cfg))
    a=w.inbox.enqueue(dict(operation='a'));b=w.inbox.enqueue(dict(operation='b'))
    register(w,a,100,200,'route-a');register(w,b,110,180,'route-b')
    w.inbox.put('service:'+a,dict(service_job_id='aa'*32,deadline='190'))
    first=select(w,[(a,dict(height=1,depth=6))],120)
    attempt(w,first,'attempt-1')
    second=select(w,[(a,dict(height=1,depth=6)),(b,dict(height=2,depth=6))],130)
    assert second['id']==first['id'] and second['deadline']==180
    assert second['need_ids']==sorted([a,b]) and second['payment_jobs']==['aa'*32]
    assert second['attempts']==['attempt-1']
    with pytest.raises(ValueError,match='ORIGINAL_DEADLINE_CHANGED'):register(w,a,100,300,'route-a')
    register(w,a,140,400,'another-delivery');assert w.inbox.get('obligation:'+a)['deadline']==200
    w.inbox.close();w.inbox=Inbox(cfg)
    assert select(w,[(a,{})],150)['id']==first['id']
    w.inbox.status(a,'READY');w.inbox.status(b,'COMPLETED');select(w,[],160)
    assert w.inbox.get('execution_group')['state']=='COVERED'
    assert w.inbox.get('service:'+a)['service_job_id']=='aa'*32
    w.inbox.close()


def test_request_validation_accepts_old_state_hint_but_rejects_scope_expiry_and_reason(tmp_path):
    from signer_agent.zk_worker_transport import validate_request
    c,recipient,peer,block,board=fake_client(tmp_path)
    need=dict(kind='deposit',consumer=Web3.to_checksum_address('0x'+'88'*20),txid='bb'*32,vout='1')
    _,p=c.send_request(need,'fed',recipient)
    record=dict(header=p['header'],body=p['body'],route=dict(requester=c.account.address,service=recipient,federation='fed',request_id=p['request_id']))
    assert validate_request(c,record,100)[0]==need
    assert validate_request(c,record,100000)[2]==int(p['body']['service_deadline'])  # Canonical publication was timely; delayed read never resets SLA.
    for field,value,error in [('state','0x'+'00'*20,'SCOPE'),('reason','WITHDRAWAL','REASON'),('deliver_by','0','EXPIRED')]:
        changed=deepcopy(record);changed['body'][field]=value
        with pytest.raises(ValueError,match=error):validate_request(c,changed,100)
    old=deepcopy(record);old['body']['known_state']['version']='900'
    assert validate_request(c,old,100)[0]==need  # State hint never fixes the proving base.
    c.close()


def test_ack_projection_and_reorg_do_not_authorize_financial_readiness(tmp_path):
    import hashlib
    c,recipient,peer,block,board=fake_client(tmp_path)
    need=dict(kind='deposit',consumer=Web3.to_checksum_address('0x'+'88'*20),txid='bb'*32,vout='1')
    _,p=c.send_request(need,'fed',recipient)
    h=dict(p['header'],kind='ACK',sender=recipient,recipient=c.account.address)
    key=message_key(h['route'],'ACK','0');cipher=b'unit canonical ciphertext'
    body=dict(version='1',request_id=p['request_id'],operation_id=p['body']['operation_id'],status='ACCEPTED',service_deadline=p['body']['service_deadline'])
    record=dict(header=h,body=body,ciphertext_sha256=hashlib.sha256(cipher).hexdigest())
    c.store.db.execute("INSERT INTO incoming VALUES(?,?,'RECEIVED')",(key,json.dumps(record)))
    state=dict(cipher=cipher)
    board.functions.packet=lambda k:NS(call=lambda **kw:[bytes(32),1,0,recipient,c.account.address,1,1,1000,state['cipher']])
    c.process_replies()
    projection=c.store.get('reply:'+h['route'])
    assert projection['status']=='ACCEPTED' and projection['acknowledged']
    state['cipher']=b'';c.process_replies()
    assert c.store.get('reply:'+h['route'])['status']=='DELIVERY_REVALIDATION_REQUIRED'
    assert not c.store.get('reply:'+h['route'])['acknowledged']
    c.close()


def test_worker_groups_two_ready_needs_into_one_prepared_proof(tmp_path,monkeypatch):
    from test_zk_worker import make_worker,simulated
    w=make_worker(tmp_path);model=simulated(w,monkeypatch,tmp_path)
    w.c['batching']=dict(local_service_seconds='300')
    old=w.context
    def context(**kwargs):
        block,*rest=old(**kwargs);block.timestamp=1000;return (block,*rest)
    w.context=context
    a=w.inbox.enqueue(dict(kind='deposit',consumer='unit',txid='11'*32,vout='0'))
    b=w.inbox.enqueue(dict(kind='deposit',consumer='unit',txid='22'*32,vout='0'))
    register(w,a,1000,1300,'a');register(w,b,1000,1200,'b')
    assert w.tick()['status']=='PROOF_PREPARED'
    active=w.inbox.get('attempt')
    assert active['need_ids']==[a,b] and active['service_deadline']==1200
    assert w.tick()['status']=='STATE_PROGRESS'
    assert w.tick()['status']=='HEADER_PROGRESS'
    assert w.tick()['status']=='IDLE'
    assert model['proves']==1 and w.inbox.get('execution_group')['state']=='COVERED'
    w.inbox.close()


def test_v3_worker_explicit_transport_mapping_is_scope_bound(tmp_path):
    from test_zk_worker_service import worker
    from signer_agent.zk_worker import load_config as load_worker
    w=worker(tmp_path);c=deepcopy(w.c);w.inbox.close()
    c['version']='3';c['batching']=dict(local_service_seconds='300')
    consumer=next(iter(c['allowed_consumers']))
    c['transport']=dict(config_file=str(tmp_path/'transport.json'),federation_consumers={'fed':[consumer]},status_interval_seconds='60')
    p=tmp_path/'worker.json';write(p,c)
    assert load_worker(p)==c
    c['transport']['federation_consumers']['fed']=[Web3.to_checksum_address('0x'+'99'*20)]
    write(p,c)
    with pytest.raises(ValueError,match='TRANSPORT_CONSUMERS'):load_worker(p)

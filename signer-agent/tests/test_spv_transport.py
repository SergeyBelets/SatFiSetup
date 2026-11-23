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
    c.pop('history_max_bytes')  # Most fixtures exercise the unchanged legacy policy.
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


def fake_client(tmp_path, **limits):
    c,key=client_config(tmp_path);c.update(limits);client=Client(c,w3=NS())
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


def test_queued_rpc_lag_keeps_the_existing_wire_status(tmp_path, monkeypatch):
    from signer_agent.zk_worker_transport import WorkerTransport

    cfg = worker_config(tmp_path)
    inbox = Inbox(cfg)
    key = inbox.enqueue(dict(kind="withdrawal", consumer="fixture"))
    inbox.status(key, "WAITING_BITCOIN_RPC")
    inbox.put("remote:route", dict(local_need_id=key, request_id="request",
        operation_id="operation", deadline=200))
    c, _, _, _, _ = fake_client(tmp_path)
    replies = []

    def queued(route, kind, sequence, body):
        assert (route, kind, sequence) == ("route", "STATUS", 1)
        replies.append(body)

    monkeypatch.setattr(c, "queue_reply", queued)
    monkeypatch.setattr(c.store, "outgoing", lambda _: (None, dict(body=replies[-1])))
    transport = WorkerTransport.__new__(WorkerTransport)
    transport.client = c
    transport.worker = NS(inbox=inbox, c=dict(cfg, transport=dict(status_interval_seconds="30")))
    transport.updates(dict(status="WAITING_BITCOIN_RPC"))
    assert replies[0]["status"] == "OBSERVATION_UNAVAILABLE"
    assert replies[0]["result"] == "WAITING_BITCOIN_RPC"
    c.close(); inbox.close()


def test_rpc_behind_does_not_requeue_messages_or_rewind_cursor(tmp_path):
    client, recipient, _, block, board = fake_client(tmp_path)
    need = dict(kind="deposit", consumer=Web3.to_checksum_address("0x" + "88" * 20), txid="bb" * 32, vout="1")
    key, _ = client.send_request(need, "fed", recipient)
    board.functions.messageCount = lambda _address: NS(call=lambda **_kw: 0)
    client.store.db.execute("UPDATE outgoing SET state='CONFIRMED' WHERE id=?", (key,))
    prior = dict(number=2, hash=("22" * 32))
    cursor = dict(prior, index=1)
    client.store.put("outgoing_anchor", prior)
    client.store.put("cursor", cursor)
    client.send = lambda *_a, **_kw: pytest.fail("RPC lag must never trigger a send")
    try:
        for action in (client.transmit, client.poll, client.process_replies):
            with pytest.raises(ValueError, match="^EVM_RPC_BEHIND$"):
                action()
        assert client.store.get("outgoing_anchor") == prior
        assert client.store.get("cursor") == cursor
        assert client.store.db.execute("SELECT state FROM outgoing WHERE id=?", (key,)).fetchone()[0] == "CONFIRMED"
    finally:
        client.close()


def test_different_block_hash_can_rewind_message_cursor(tmp_path):
    client, _, _, block, board = fake_client(tmp_path)
    board.functions.messageCount = lambda _address: NS(call=lambda **_kw: 0)
    client.store.put("cursor", dict(number=block.number, hash="22" * 32, index=1))
    try:
        assert client.poll() == 0
        assert client.store.get("cursor") == dict(number=block.number, hash=block.hash.hex(), index=0)
    finally:
        client.close()


def test_confirmed_packet_history_releases_active_slots_and_preserves_budgets(tmp_path, monkeypatch):
    cfg, _ = client_config(tmp_path)
    cfg.update(max_pending_messages="1", max_transaction_cost_wei="8", max_total_cost_wei="8",
               history_max_bytes=str(64 * 2**20))
    filename = tmp_path / "configured.json"
    write(filename, cfg)
    assert load_config(filename) == cfg
    sender = header()["sender"]
    store = Store(cfg, sender)
    pending = dict(header=header(), ciphertext="aa" * 80, body={})
    published = store.queue("first", pending)
    store.db.execute("UPDATE outgoing SET state='CONFIRMED' WHERE id=?", (published,))
    store.reserve("first", 8)
    queued = dict(pending, header=dict(header(), route="ab" * 32))
    try:
        assert store.queue("second", queued) != published
        assert store.history_status()["active_outgoing"] == 1
        store.close()
        store = Store(cfg, sender)
        assert store.outgoing("first") == (published, pending, "CONFIRMED")
        assert store.queue("first", queued) == published
        with pytest.raises(ValueError, match="^SPV_OUTBOX_FULL$"):
            store.queue("third", pending)
        with pytest.raises(ValueError, match="^SPV_TRANSPORT_GAS_BUDGET$"):
            store.reserve("new", 1)
        with monkeypatch.context() as patch:
            patch.setattr("signer_agent.spv_history.sqlite_bytes", lambda *_: int(cfg["history_max_bytes"]))
            assert store.queue("first", queued) == published
            with pytest.raises(ValueError, match="^SPV_HISTORY_STORAGE_LIMIT$"):
                store.queue("third", pending)
    finally:
        store.close()


def test_archived_replies_are_rechecked_in_bounded_pages_after_confirmed_reorg(tmp_path):
    import hashlib

    client, receiver, _, block, board = fake_client(tmp_path, history_max_bytes=str(64 * 2**20),
                                                    max_messages_per_poll="1")
    records = []
    packets = {}
    body = dict(kind="deposit", consumer=Web3.to_checksum_address("0x" + "88" * 20), txid="bb" * 32, vout="1")
    for revision in ("0", "1"):
        _, original = client.send_request(body, "fed", receiver, revision=revision)
        h = dict(original["header"], kind="ACK", sender=receiver, recipient=client.account.address)
        message = message_key(h["route"], "ACK", "0")
        record = dict(header=h, body=dict(version="1", request_id=original["request_id"], status="ACCEPTED",
            operation_id=original["body"]["operation_id"]), ciphertext_sha256=hashlib.sha256(b"packet").hexdigest())
        client.store.db.execute("INSERT INTO incoming VALUES(?,?,'RECEIVED')", (message, json.dumps(record)))
        records.append((message, h["route"]))
        packets[message] = b"packet"
    queried = []

    def packet(message):
        queried.append(message.hex())
        return NS(call=lambda **_: [None] * 8 + [packets.get(message.hex(), b"")])

    board.functions.packet = packet
    try:
        with pytest.raises(ValueError, match="^SPV_REPLY_HISTORY_RECHECK_REQUIRED$"):
            client.process_replies()
        client.process_replies()
        client.process_replies()
        assert queried == [message for message, _ in records]
        doomed, route = records[0]
        packets.pop(doomed)
        block.hash = b"b" * 32
        with pytest.raises(ValueError, match="^SPV_REPLY_HISTORY_RECHECK_REQUIRED$"):
            client.process_replies()
        assert not client.store.get("reply:" + route)
        client.process_replies()
        assert client.store.get("reply:" + records[1][1])["acknowledged"]
        assert not client.store.get("reply-history-recheck")
        assert len(client.store.incoming("ACK")) == 2
        assert queried == [message for message, _ in records] * 2
    finally:
        client.close()


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

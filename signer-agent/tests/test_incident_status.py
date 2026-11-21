"""Real HPKE/ABI/SQLite; canonical EVM, quorum and Core boundaries are explicit fixtures."""
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
import json
import pytest
from eth_utils import keccak
from eth_abi import encode
from signer_agent import incident_transport as t,address_status as p,address_mint,address_registration
from signer_agent.address_status_client import Driver
from signer_agent.address_watch import digest
from signer_agent.operations import canonical
from test_address_mint import Functions,address
from test_address_registration import registered_env,custody_profile
from test_address_transport import delivery


@pytest.fixture
def incident(registered_env,monkeypatch):
    e=registered_env;e.status_receipts={};e.executed=set();e.custody_gen=1;e.derivation_gen=1;e.custody_nonce=0;e.derivation_nonce=0;e.closed=False
    e.reg_values.update(custodyScope=b'c'*32,derivationScope=b'd'*32,
        custodyGeneration=lambda:e.custody_gen,derivationGeneration=lambda:e.derivation_gen,
        custodyStatusNonce=lambda:e.custody_nonce,derivationStatusNonce=lambda:e.derivation_nonce,
        issuanceClosed=lambda:e.closed,statusReceipts=lambda op:e.status_receipts.get(bytes(op),0))
    e.reg_values['getStatusOperationHash']=lambda c:p.operation_hash(e.w3,e.ctx,e.auth,c,20)
    e.auth.functions.values.update(executedOperations=lambda op:bytes(op) in e.executed,operationTargets=lambda op:e.reg.address,
        consumedSessions=True)
    e.inc_packets={};e.inc_routes={};e.inc_inboxes={a.address:[] for a in e.accounts}
    e.inc_domain=keccak(encode(['bytes32','uint256','address','address','bytes32','address'],[
        keccak(text='SATOHSI_INCIDENT_MESSAGES_V1'),31337,address(50),e.reg.address,e.reg_values['graphId'],e.auth.address])).hex()
    values=dict(e.board.functions.values)
    values.update(registry=e.reg.address,authorization=e.auth.address,incidentDomain=bytes.fromhex(e.inc_domain),
        packet=lambda key:e.inc_packets.get(bytes(key).hex(),e.blank),
        route=lambda route:e.inc_routes.get(bytes(route).hex(),(t.ZERO,t.ZERO,bytes(32))),
        messageCount=lambda who:len(e.inc_inboxes[who]),messageAt=lambda who,index:bytes.fromhex(e.inc_inboxes[who][index]),
        serviceAllowed=lambda who:who in [a.address for a in e.accounts])
    e.inc_board=SimpleNamespace(address=address(50),functions=Functions(values))
    original=e.w3.eth.contract
    e.w3.eth.contract=lambda address,**kw:e.inc_board if address==e.inc_board.address else original(address=address,**kw)
    monkeypatch.setattr(t,'checked_runtime',lambda *_:None);monkeypatch.setattr(t,'prove_creation',lambda *_:None)
    monkeypatch.setattr(address_mint,'configuration',lambda *a,**kw:e.ctx)
    monkeypatch.setattr(t.Client,'context',lambda c:(e.block(20),e.pool,e.ctx,e.inc_board,e.inc_domain))
    configs=[dict(c,board=e.inc_board.address,directory=str(e.tmp_path/f'incident-{i}'),recovery_service_config=None) for i,c in enumerate(e.configs)]
    e.inc_clients=[t.Client(c,e.w3) for c in configs];e.reporter,e.operator=e.inc_clients
    old_send=e.send;e.lose_session_write=False;e.lose_status_write=False
    def send(key,method,args,**kwargs):
        if method!='setStatus':return old_send(key,method,args,**kwargs)
        e.sent.append((key,method,args,kwargs));c=p.parse_change(args[0]);op=bytes.fromhex(args[1][2:])
        assert op in e.approved
        if op not in e.status_receipts:
            p.pending(e.w3,e.ctx,c,20)
            if c[0]==0:
                e.view.custody=c[4];e.custody_gen+=1;e.custody_nonce+=1;e.closed=e.closed or c[5] or c[4] in (1,3)
                generation=e.custody_gen
            else:e.view.derivation=c[4];e.derivation_gen+=1;e.derivation_nonce+=1;generation=e.derivation_gen
            e.status_receipts[op]=generation;e.executed.add(op)
        if e.lose_status_write:raise SystemExit('crash after canonical status receipt')
        return {}
    for c in e.inc_clients:c.send=send
    def publish(c):
        with c.store.journal.db() as db:rows=db.execute("SELECT id,body FROM address_packets WHERE direction='OUT'").fetchall()
        for key,encoded in rows:
            packet=json.loads(encoded);h=packet['header']
            if key in e.inc_packets:continue
            if h['kind']=='REQUEST':e.inc_routes[h['route']]=(h['sender'],h['recipient'],bytes.fromhex(packet['request_id']))
            e.inc_packets[key]=(bytes.fromhex(h['route']),{'REQUEST':0,'ACK':1,'STATUS':2}[h['kind']],int(h['sequence']),
                h['sender'],h['recipient'],int(h['sender_epoch']),int(h['recipient_epoch']),int(h['expires_at']),bytes.fromhex(packet['ciphertext']))
            e.inc_inboxes[h['recipient']].append(key)
    e.inc_publish=publish
    def report(scope='0'):
        r=e.reporter.prepare_report(scope,'ab'*32,dict(summary='private operator evidence',references=['cd'*32]),incident_id='ef'*32)
        result=e.reporter.send_report(r,e.operator.account.address);publish(e.reporter);e.operator.receive_once()
        return r,result
    e.new_report=report;e.status_driver=Driver(e.operator,e.registration_board.address)
    return e


def proposed(e,scope='0',next_state='SUSPENDED',deadline='1600'):
    report,delivery=e.new_report(scope)
    prepared=e.status_driver.prepare(t.report_id(report),next_state,False,deadline)
    identifier=prepared['operation_id'];result=e.status_driver.propose(identifier)
    return identifier,result['intent'],report,delivery


@pytest.mark.parametrize('foreign_creator',[False,True])
def test_custodian_can_publish_status_without_original_operator(incident,foreign_creator):
    e=incident;custody_profile(e)
    identifier,intent,_,_=proposed(e)
    assert p.review(e.w3,intent,e.bundle,20)['session']==1
    assert not e.approved and not e.status_receipts
    if foreign_creator:
        e.sessions[1][1]=address(100)
        with pytest.raises(ValueError,match='INCIDENT_STATUS_SESSION'):p.review(e.w3,intent,e.bundle,20)


def test_encrypted_report_durable_ack_and_forward_keep_original_reporter(incident):
    e=incident;r,delivery=e.new_report()
    assert not e.status_receipts and e.view.custody==0
    packet=e.inc_packets[delivery['message_id']]
    assert b'private operator evidence' not in bytes(packet[8])
    e.inc_publish(e.operator);e.reporter.receive_once()
    assert e.reporter.store.get('incident-reply:'+next(k for k,pkt in e.inc_packets.items() if pkt[1]==1))['type']=='INCIDENT_ACK'
    # Forwarding is encrypted by an authorized participant, retaining the
    # reporter's original canonical commitment rather than asserting authorship.
    forwarded=e.operator.send_report(r,e.reporter.account.address);e.inc_publish(e.operator);e.reporter.receive_once()
    assert forwarded['origin']['message_id']==delivery['message_id']
    assert e.reporter.report(t.report_id(r))==r
    assert e.reporter.proof(t.report_id(r),e.ctx,20)['message_id']==delivery['message_id']


def test_incident_hpke_is_separate_from_address_requests(incident):
    from signer_agent.address_transport import open_packet
    e=incident;r,delivery=e.new_report();packet=e.inc_packets[delivery['message_id']]
    with e.reporter.store.journal.db() as db:wire=json.loads(db.execute("SELECT body FROM address_packets WHERE id=?",(delivery['message_id'],)).fetchone()[0])
    with pytest.raises(RuntimeError):open_packet(e.inc_domain,wire['header'],bytes(packet[8]).hex(),e.keys[1]['secret'])


def test_status_native_hash_schema_and_public_intent_do_not_disclose_details(incident):
    e=incident;identifier,intent,r,_=proposed(e)
    assert 'private operator evidence' not in json.dumps(intent) and 'salt' not in json.dumps(intent)
    c=p.parse_change(intent['hash_args'][0])
    assert c[:7]==[0,0,1,0,2,False,1600]
    checked=p.review(e.w3,intent,e.bundle,20)
    assert checked['operation_hash'].hex()==intent['operation_hash']
    bad=deepcopy(intent);bad['hash_args'][0][7][2]='0x'+'11'*32
    with pytest.raises(ValueError,match='STATUS_HASH'):p.review(e.w3,bad,e.bundle,20)
    assert e.status_driver.propose(identifier)['session']==intent['session']
    assert sum(c[1]=='createSessionFromDKG' for c in e.sent)==1


def test_signing_guard_requires_private_evidence_exact_approval_and_current_generation(incident,monkeypatch):
    e=incident;identifier,intent,r,_=proposed(e)
    participant=e.operator
    coordinator=SimpleNamespace(sender=participant.account.address,address=e.coordinator.address,w3=e.w3,
        _read_snapshot=e.block(20),check_read_snapshot=lambda:None)
    session=SimpleNamespace(session_id=int(intent['session']),message_hash=bytes.fromhex(intent['operation_hash']),origin_contract=e.reg.address)
    coordinator.incident_status_context=(intent,e.bundle,participant)
    monkeypatch.setattr(address_registration,'checked_runtime',lambda *_:None)
    with pytest.raises(ValueError,match='LOCAL_APPROVAL_REQUIRED'):address_registration.guard_signing_session(coordinator,session)
    q=e.status_driver.review(identifier)
    with pytest.raises(ValueError,match='CONFIRMATION_REQUIRED'):e.status_driver.approve(identifier,'00'*32)
    e.status_driver.approve(identifier,q['confirmation_hash'])
    address_registration.guard_signing_session(coordinator,session)
    e.custody_gen+=1
    with pytest.raises(ValueError,match='GENERATION_CHANGED'):address_registration.guard_signing_session(coordinator,session)


def test_lost_status_write_recovers_exact_historical_receipt_without_notes_or_new_send(incident):
    e=incident;identifier,intent,r,_=proposed(e)
    e.lose_status_write=True
    with pytest.raises(SystemExit):e.status_driver.execute(identifier)
    assert e.custody_gen==2 and e.view.custody==2
    e.lose_status_write=False;e.view.timestamp=9999;e.custody_gen=5;e.view.custody=3
    with e.operator.store.journal.db() as db:db.execute("DELETE FROM address_meta WHERE id LIKE 'incident-report:%'")
    result=e.status_driver.execute(identifier)
    assert result['status']=='STATUS_APPLIED_HISTORICAL' and result['current']['state']==3
    assert sum(c[1]=='setStatus' for c in e.sent)==1
    assert p.review(e.w3,intent,e.bundle,20)['generation']==2


def test_lost_session_metadata_write_replays_same_session_envelope(incident,monkeypatch):
    e=incident;r,_=e.new_report();identifier=e.status_driver.prepare(t.report_id(r),'SUSPENDED',False,'1600')['operation_id']
    original=e.status_driver._put
    def crash(record,b):
        if record['attempts'][-1]['session'] is not None:raise SystemExit('before local session write')
        return original(record,b)
    monkeypatch.setattr(e.status_driver,'_put',crash)
    with pytest.raises(SystemExit):e.status_driver.propose(identifier)
    assert len(e.sessions)==1
    monkeypatch.setattr(e.status_driver,'_put',original)
    assert e.status_driver.propose(identifier)['session']=='1'
    assert len(e.sessions)==1


def test_stale_generation_requires_explicit_retry_and_new_local_approval(incident):
    e=incident;identifier,intent,_,_=proposed(e)
    q=e.status_driver.review(identifier);e.status_driver.approve(identifier,q['confirmation_hash'])
    e.custody_gen+=1;e.custody_nonce+=1;e.view.custody=2
    with pytest.raises(ValueError,match='GENERATION_CHANGED'):e.status_driver.execute(identifier)
    retried=e.status_driver.retry(identifier,'1700')
    assert retried['attempt']=='2' and retried['session'] is None
    new=e.status_driver.propose(identifier)['intent']
    assert new['operation_hash']!=intent['operation_hash']
    with pytest.raises(ValueError,match='LOCAL_APPROVAL_REQUIRED'):p.private_review(e.operator,new,e.bundle,20,approval=True)


@pytest.mark.parametrize('scope,state,next_state,closed',[('0',3,'ACTIVE',False),('0',2,'ACTIVE',True),('1',2,'QUALIFIED',False)])
def test_terminal_and_closed_issuance_cannot_be_resumed(incident,scope,state,next_state,closed):
    e=incident;e.view.custody=state if scope=='0' else 0;e.view.derivation=state if scope=='1' else 1;e.closed=closed
    r,_=e.new_report(scope)
    with pytest.raises(ValueError,match='STATUS_TERMINAL'):e.status_driver.prepare(t.report_id(r),next_state,False,'1600')


def test_orphaned_session_or_approval_needs_explicit_revalidation(incident):
    e=incident;identifier,intent,_,_=proposed(e)
    q=e.status_driver.review(identifier);e.status_driver.approve(identifier,q['confirmation_hash'])
    e.view.reorg=True
    with pytest.raises(ValueError,match='EVM_REORG'):e.status_driver.execute(identifier)
    assert e.status_driver.recover(identifier)['status']=='INCIDENT_REVALIDATION_REQUIRED'
    with pytest.raises(ValueError,match='LOCAL_APPROVAL_REQUIRED'):p.private_review(e.operator,intent,e.bundle,20,approval=True)
    q=e.status_driver.review(identifier);e.status_driver.approve(identifier,q['confirmation_hash'])
    assert p.private_review(e.operator,intent,e.bundle,20,approval=True)['complete'] is False


def test_report_tamper_is_quarantined_and_transport_reorg_cannot_authorize_new_status(incident):
    e=incident;r,delivery=e.new_report();identifier=t.report_id(r)
    corrupted=deepcopy(r);corrupted['details']['summary']='substituted'
    with pytest.raises(ValueError,match='EVIDENCE_MISMATCH'):e.operator._save_report(corrupted,e.ctx,20)
    e.inc_packets.pop(delivery['message_id'])
    with pytest.raises(ValueError,match='DELIVERY_REQUIRED'):e.status_driver.prepare(identifier,'SUSPENDED',False,'1600')


def test_local_backup_preserves_private_report_without_rolling_back_transaction_lane(incident,tmp_path):
    from signer_agent.operations import Journal
    e=incident;_,intent,r,_=proposed(e);path=tmp_path/'incident-backup.sqlite'
    identity=e.operator.c['identity_file'];lane=Journal(identity+'.transactions.sqlite')
    lane.put('sentinel',dict(spent='retained'))
    e.operator.store.backup(path)
    assert path.is_file() and lane.get('sentinel')['request']['spent']=='retained'
    assert e.operator.report(t.report_id(r))==r


def test_resume_requires_service_revalidation_without_clearing_signing_pause(incident):
    e=incident;e.view.custody=2
    identifier,intent,_,_=proposed(e,next_state='ACTIVE')
    with pytest.raises(ValueError,match='SERVICE_REVIEW_REQUIRED'):e.status_driver.review(identifier)
    assert not e.status_receipts


def test_resume_checks_complete_watch_against_core_and_retains_existing_journals(incident,monkeypatch):
    from test_address_transport import private
    from signer_agent import bitcoin_rpc
    e=incident;e.view.custody=2
    identifier,intent,_,_=proposed(e,next_state='ACTIVE')
    service_config=dict(e.configs[1],bitcoin_rpc='http://127.0.0.1:18443',bitcoin_cookie=str(e.tmp_path/'cookie'))
    filename=private(e.tmp_path/'recovery.json',service_config);e.operator.c['recovery_service_config']=filename
    best='88'*32
    class Core:
        def __init__(self,*args):pass
        def check_network(self):pass
        def call(self,method,*args):
            if method=='getbestblockhash':return best
            if method=='getblockhash':return e.reg_values['bitcoinGenesis'].hex() if args[0]==0 else best
            raise AssertionError(method)
    monkeypatch.setattr(bitcoin_rpc,'BitcoinRPC',Core)
    e.done[e.intent.intent_id]=e.intent.encode()
    e.reg_values.update(intentCount=1,intentIds=lambda _:e.intent.intent_id)
    e.batch.functions.values['feeReserve']=t.ZERO
    e.service.store.put('financial-pause-sentinel',dict(retained=True))
    with pytest.raises(ValueError,match='SERVICE_REVIEW_REQUIRED'):e.status_driver.review(identifier)
    from test_address_watch import set_coverage
    set_coverage(e.service.store,e.intent.intent_id.hex(),5,best)
    q=e.status_driver.review(identifier)
    assert q['review']['service']['registered_watches']=='1'
    e.status_driver.approve(identifier,q['confirmation_hash'])
    assert e.service.store.get('financial-pause-sentinel')==dict(retained=True)
    assert e.status_driver.execute(identifier)['status']=='STATUS_APPLIED'


def test_unqualified_derivation_cannot_be_resumed(incident,monkeypatch):
    from signer_agent import bitcoin_trust,contract_provenance
    e=incident;e.view.derivation=1;e.view.qualified=False
    monkeypatch.setattr(bitcoin_trust,'checked_runtime',lambda *_:None)
    monkeypatch.setattr(contract_provenance,'prove_creation',lambda *_:None)
    r,_=e.new_report('1')
    with pytest.raises(ValueError,match='DERIVATION_UNQUALIFIED'):e.status_driver.prepare(t.report_id(r),'QUALIFIED',False,'1600')


def test_status_graph_can_be_inspected_when_verifier_is_broken_without_weakening_mint(monkeypatch):
    from signer_agent.deposit_intent import PROFILE
    values=dict(deposits=address(1),authorization=address(2),escrow=address(3),batches=address(4),dkg=address(5),
        chainId=31337,PROFILE=keccak(text=PROFILE),verifier=address(7),bitcoinGenesis=b'g'*32,
        verifierRuntimeHash=b'h'*32,verifierSourceHash=b's'*32,qualificationCommitment=b'q'*32,
        servicePolicyHash=b'p'*32,graphId=b'c'*32,custodyScope=b'u'*32,derivationScope=b'd'*32)
    registry=SimpleNamespace(address=address(6),functions=Functions(values))
    w3=SimpleNamespace(eth=SimpleNamespace(chain_id=31337,contract=lambda **kw:registry))
    def runtime(w3,where,kind,number):
        if kind=='BitcoinDepositDerivation':raise ValueError('BTC_CONTRACT_BUILD_MISMATCH')
    monkeypatch.setattr(address_mint,'checked_runtime',runtime)
    proofs=[];monkeypatch.setattr(address_mint,'prove_creation',lambda *a:proofs.append(a) or {})
    args=[w3,address(6),address(1),address(2),address(3),address(4),address(5),20]
    with pytest.raises(ValueError,match='BUILD_MISMATCH'):address_mint.registry_identity(*args)
    assert address_mint.registry_identity(*args,verify_derivation=False)[0] is registry
    assert proofs[-1][2]=='BitcoinAddressRegistry'
    args[3]=address(99)
    with pytest.raises(ValueError,match='GRAPH_MISMATCH'):address_mint.registry_identity(*args,verify_derivation=False)


def test_notify_is_encrypted_observation_without_status_authority(incident):
    e=incident;r,_=e.new_report()
    assert e.operator.notify(t.report_id(r))['count']==1
    e.inc_publish(e.operator);e.reporter.receive_once()
    with e.reporter.store.journal.db() as db:replies=db.execute("SELECT body FROM address_meta WHERE id LIKE 'incident-reply:%'").fetchall()
    assert any(json.loads(row[0])['type']=='INCIDENT_STATUS' for row in replies)
    assert e.view.custody==0 and not e.status_receipts


def test_malformed_encrypted_report_is_quarantined(incident):
    e=incident
    r=e.reporter.prepare_report('0','ab'*32,dict(summary='confidential',references=[]))
    sent=e.reporter.send_report(r,e.operator.account.address)
    with e.reporter.store.journal.db() as db:
        row=db.execute("SELECT body FROM address_packets WHERE id=?",(sent['message_id'],)).fetchone();packet=json.loads(row[0])
        packet['body']['report']['scope']=None
        packet['ciphertext']=e.reporter.seal_packet(e.inc_domain,packet['header'],packet['body'],e.keys[1]['public'])
        db.execute('UPDATE address_packets SET body=? WHERE id=?',(canonical(packet),sent['message_id']))
    e.inc_publish(e.reporter)
    assert e.operator.receive_once()['status']=='INCIDENT_MESSAGES_SAVED'
    assert e.operator.store.get('incident-rejected:'+sent['message_id'])['reason']=='INCIDENT_REPORT'


def test_explicit_delivery_retry_keeps_evidence_and_changes_only_delivery_identity(incident):
    e=incident;r=e.reporter.prepare_report('0','ab'*32,dict(summary='retained evidence',references=[]))
    first=e.reporter.send_report(r,e.operator.account.address)
    e.view.timestamp+=700
    retry=e.reporter.send_report(r,e.operator.account.address,'1')
    assert retry['report_id']==first['report_id'] and retry['route']!=first['route']
    # The old unsent envelope remains intact, with its original deadline/keys.
    e.inc_publish(e.reporter);e.operator.receive_once()
    proof=e.operator.proof(t.report_id(r),e.ctx,20)
    assert e.operator.report(t.report_id(r))==r
    t.delivery_proof(e.w3,e.ctx,e.inc_board.address,t.public_report(r),retry['message_id'],20,attempt='1')
    with pytest.raises(ValueError,match='DELIVERY_REQUIRED'):
        t.delivery_proof(e.w3,e.ctx,e.inc_board.address,t.public_report(r),retry['message_id'],20)
    with pytest.raises(ValueError):e.reporter.send_report(r,e.operator.account.address,'32')


def test_frozen_status_hash_matches_independent_solidity_abi_vector():
    vector=json.loads((Path(__file__).resolve().parents[2]/'web3/test/fixtures/incident-status-v1.json').read_text())
    root=SimpleNamespace(address=vector['registry'],functions=Functions(dict(custodyScope=bytes.fromhex(vector['scope']))))
    w3=SimpleNamespace(eth=SimpleNamespace(chain_id=int(vector['chain_id'])))
    auth=SimpleNamespace(address=vector['authorization']);c=p.parse_change(vector['change'])
    assert '0x'+p.operation_hash(w3,dict(registry=root),auth,c,20).hex()==vector['operation_hash']
    assert '0x'+keccak(encode([p.CHANGE_TYPE],[c])).hex()==vector['abi_hash']


def test_status_digest_uses_existing_native_frost_nonce_journal_and_real_signature(incident,tmp_path):
    """Fixed polynomial test shares; real native adapter and libsecp verification.

    Coordinator/DKG provenance remain the explicit fixture, not a live ceremony.
    """
    import time
    from dataclasses import replace
    from coincurve import PublicKeyXOnly
    from signer_agent.coordinator import Session
    from signer_agent.service import SigningService
    from signer_agent.secure_state import SecureState
    from signer_agent.frost_math import scalar_base_mult,build_transcript,aggregate_nonce,negate_point
    from test_signing_service import FakeClient,_config
    e=incident;e.view.timestamp=int(time.time());secret=23
    while scalar_base_mult(secret).y&1:secret+=1
    group=scalar_base_mult(secret).x.to_bytes(32,'big')
    e.auth.functions.values['groupPubkey']=group
    completed=list(e.dkg.functions.values['getCompletedResult']);completed[4]=group
    e.dkg.functions.values['getCompletedResult']=tuple(completed)
    identifier,intent,_,_=proposed(e,deadline=str(e.view.timestamp+3600))
    e.sessions[int(intent['session'])][2]=group
    driver=Driver(e.reporter,e.registration_board.address);driver.adopt(intent['session'])
    for d in (driver,e.status_driver):
        quote=d.review(identifier);d.approve(identifier,quote['confirmation_hash'])
    session=replace(Session.from_contract(e.sessions[int(intent['session'])]),purpose=1)
    participants=[a.address for a in e.accounts];shares=[secret+17,secret+34]
    public={who:scalar_base_mult(share) for who,share in zip(participants,shares)}
    shared=dict(commitments={},shares={},refused=set());services=[]
    for index,(who,share,inbox) in enumerate(zip(participants,shares,e.inc_clients),1):
        config=replace(_config(tmp_path,who,share,index),coordinator_address=e.coordinator.address)
        client=FakeClient(who,participants,public,shared)
        client.address=e.coordinator.address;client.w3=e.w3;client._read_snapshot=e.block(20);client.check_read_snapshot=lambda:None
        client.guard_address_registration_session=lambda s,c=client,i=inbox:p.guard_signing_session(c,s,(intent,e.bundle,i))
        services.append(SigningService(config,client,SecureState(config.state_path,config.state_key)))
    for service in services:assert service.approve(session).status=='committed'
    # Reload a participant's state between rounds, retaining the independent journal.
    services[0]=SigningService(services[0].config,services[0].client,SecureState(services[0].config.state_path,services[0].config.state_key))
    transcript=build_transcript(31337,e.coordinator.address,session.session_id,group,session.message_hash,participants,shared['commitments'])
    aggregate=aggregate_nonce(transcript,session.session_id,participants,shared['commitments'])
    shared.update(transcript=transcript,locked_aggregate=negate_point(aggregate) if aggregate.y&1 else aggregate)
    for service in services:assert service.approve(session).status=='signed'
    assert services[0].try_finalize(session).status=='finalized'
    signature=shared['finalized'][0]
    assert PublicKeyXOnly(group).verify(signature,session.message_hash)
    assert not PublicKeyXOnly(group).verify(signature,keccak(text='changed status'))
    for service in services:
        key=SecureState.session_key(31337,e.coordinator.address,session.session_id)
        assert service.state.get(key)['status']=='consumed'


def test_unconfirmed_status_receipt_is_observation_and_never_a_confirmed_pause(incident):
    e=incident;identifier,intent,_,_=proposed(e);op=bytes.fromhex(intent['operation_hash'])
    e.reg.functions.statusReceipts=lambda key:SimpleNamespace(call=lambda block_identifier:e.status_receipts.get(bytes(key),0) if block_identifier=='latest' else 0)
    e.status_receipts[op]=2
    before=list(e.sent)
    assert e.status_driver.status(identifier)['status']=='STATUS_CONFIRMING'
    assert e.status_driver.execute(identifier)['status']=='STATUS_CONFIRMING'
    assert e.status_driver.retry(identifier,'1700')['status']=='STATUS_CONFIRMING'
    assert e.sent==before


def test_authorized_relayer_can_deliver_without_private_report(incident):
    e=incident;identifier,intent,_,_=proposed(e)
    e.approved.add(bytes.fromhex(intent['operation_hash']))
    with e.operator.store.journal.db() as db:db.execute("DELETE FROM address_meta WHERE id LIKE 'incident-report:%'")
    assert e.status_driver.execute(identifier)['status']=='STATUS_APPLIED'


def test_bitcoin_incident_cli_writes_private_report_and_redacts_details(incident,monkeypatch,tmp_path,capsys):
    from signer_agent import incident_cli,bitcoin_cli
    from test_address_transport import private
    e=incident;output=tmp_path/'report.json';details=private(tmp_path/'notes.json',dict(summary='do not log this evidence',references=[]))
    monkeypatch.setattr(incident_cli,'Client',lambda _:e.reporter);monkeypatch.setattr(incident_cli,'load_config',lambda _:e.reporter.c)
    assert bitcoin_cli.main(['incident','report-prepare','--config','local','--scope','custody','--reason-code','OBSERVATION_UNAVAILABLE',
        '--details-file',details,'--output',str(output)])==0
    printed=capsys.readouterr().out
    assert 'do not log this evidence' not in printed and 'salt' not in printed
    assert output.stat().st_mode&0o077==0
    report=json.loads(output.read_text());assert report['details']['summary']=='do not log this evidence'
    assert report['reason']==keccak(text='SATOHSI_INCIDENT_REASON_V1:OBSERVATION_UNAVAILABLE').hex()


def test_generation_observation_survives_inbox_backup_and_restricts_resume_after_reorg(incident,tmp_path):
    from signer_agent.incident_observations import observe
    from signer_agent.operations import Journal
    e=incident;backup=tmp_path/'before-observation.sqlite';e.operator.store.backup(backup)
    e.custody_gen=7;e.custody_nonce=6;e.view.custody=2
    observe(e.operator,e.ctx,0,20,restrictive=True)
    e.operator.store.put('after-backup',dict(value='only inbox data'))
    import sqlite3
    with sqlite3.connect(backup) as source,e.operator.store.journal.db() as destination:source.backup(destination)
    assert e.operator.store.get('after-backup') is None
    e.custody_gen=6;e.custody_nonce=5;e.view.custody=0;e.view.reorg=True
    with pytest.raises(ValueError,match='STATUS_RECOVERY_REQUIRED'):observe(e.operator,e.ctx,0,20)
    with pytest.raises(ValueError,match='STATUS_RECOVERY_REQUIRED'):observe(e.operator,e.ctx,0,20,recover=True)
    # A newly reviewed restrictive decision may reassert the hold using the
    # canonical current generation, preserving the higher independent witness.
    result=observe(e.operator,e.ctx,0,20,restrictive=True)
    assert result['retained_generation']=='7' and result['lower_generation']
    lane=Journal(e.operator.c['identity_file']+'.transactions.sqlite')
    with lane.db() as db:
        count=db.execute('SELECT COUNT(*) FROM incident_observation_history').fetchone()[0]
    observe(e.operator,e.ctx,0,20,restrictive=True)
    with lane.db() as db:assert db.execute('SELECT COUNT(*) FROM incident_observation_history').fetchone()[0]==count
    e.custody_gen=7;e.custody_nonce=6;e.view.custody=2
    assert observe(e.operator,e.ctx,0,20,recover=True)['retained_generation']=='7'


def test_known_terminal_incident_cannot_be_erased_by_canonical_generation_or_cursor_restore(incident):
    from signer_agent.incident_observations import observe
    e=incident;e.view.custody=3;e.custody_gen=2;e.custody_nonce=1
    observe(e.operator,e.ctx,0,20,restrictive=True)
    e.view.reorg=True;e.view.custody=0;e.custody_gen=3;e.custody_nonce=2
    with pytest.raises(ValueError,match='STATUS_RECOVERY_REQUIRED'):observe(e.operator,e.ctx,0,20,recover=True)
    with pytest.raises(ValueError,match='STATUS_RECOVERY_REQUIRED'):observe(e.operator,e.ctx,0,20)
    assert observe(e.operator,e.ctx,0,20,restrictive=True)['retained_terminal']


def test_known_closed_issuance_cannot_be_reset_by_status_recovery(incident):
    from signer_agent.incident_observations import observe
    e=incident;e.closed=True;e.view.custody=1;e.custody_gen=2;e.custody_nonce=1
    observe(e.operator,e.ctx,0,20,restrictive=True)
    e.view.reorg=True;e.closed=False;e.view.custody=0;e.custody_gen=3;e.custody_nonce=2
    with pytest.raises(ValueError,match='STATUS_RECOVERY_REQUIRED'):observe(e.operator,e.ctx,0,20,recover=True)

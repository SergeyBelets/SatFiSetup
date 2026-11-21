"""Real HPKE/native intents/SQLite; EXPLICIT synthetic qualification and EVM/DKG boundaries.

These tests never qualify the actual BitcoinDepositDerivation artifact.
"""
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import pytest
from eth_utils import keccak

from signer_agent import address_registration as p,address_mint,address_transport,crypto,onchain_transport
from signer_agent.address_registration_client import Driver
from signer_agent.deposit_intent import PROFILE
from signer_agent.dkg_protocol import Manifest
from signer_agent.operations import canonical
from signer_agent.spv_transport import hex32,message_key,route_key
from test_address_transport import delivery
from test_address_mint import Functions,address


@pytest.fixture
def registered_env(delivery,monkeypatch):
    e=delivery
    e.view.qualified=True;e.view.derivation=0;e.view.cg=1;e.view.dg=1;e.view.sequence=0
    e.done={};e.approved=set();e.sessions={};e.public={};e.sent=[];e.results={}
    def node(where,values):return SimpleNamespace(address=where,functions=Functions(values))
    e.reg_values.update(custodyGeneration=lambda:e.view.cg,derivationGeneration=lambda:e.view.dg,
        registrationNonce=lambda:e.view.sequence,verifier=address(14),verifierRuntimeHash=keccak(b""),
        derivationScope=keccak(b"synthetic derivation scope"),getBinding=lambda key:(e.done.get(key,b""),))
    e.auth=node(address(7),dict(coordinator=address(12),dkgId=b"a"*32,resultHash=b"r"*32,registry=address(10),
        getParticipants=[a.address for a in e.accounts],threshold=2,groupPubkey=b"g"*32,
        isOperationApproved=lambda op,target:op in e.approved))
    e.coordinator=node(address(12),dict(dkgRegistry=address(10),defaultVerifier=address(15),getSession=lambda sid:e.sessions[sid],
        sessionDKGId=b"a"*32,sessionDKGResult=b"r"*32,
        getAuthorizationResult=lambda sid:(b"g"*32,e.sessions[sid][3],True,p.ZERO,e.reg.address,2,2,b"s"*64)))
    e.registration_board=node(address(13),dict(registry=address(10),operator=e.accounts[1].address,
        canPublishIntent=lambda _:False,intents=lambda sid:e.public.get(sid,b"")))
    e.verifier=node(address(14),dict(QUALIFIED=lambda:e.view.qualified,PROFILE=keccak(text=PROFILE)))
    e.invite=dict(version="6",bitcoin_network="regtest",evm_confirmations="1",participants=[a.address for a in e.accounts],
        threshold="2",sessions=dict(conf_ceremony=(b"a"*32).hex(),btc_ceremony=e.intent.wallet_id.hex()))
    e.bundle=dict(Manifest(31337,address(10),b"a"*32,1,"EVM_AUTH",tuple(e.invite["participants"]),
        (b"k"*32,b"l"*32),2).to_dict(),onchain_board=e.registration_board.address,role="conf_ceremony",
        onboarding=dict(invitation=e.invite))
    e.dkg=node(address(10),dict(getCompletedResult=(Manifest.from_dict(e.bundle).context,b"r"*32,bytes(32),
        keccak(text="EVM_AUTH"),b"g"*32,e.invite["participants"],2)))
    nodes={n.address:n for n in (e.pool,e.reg,e.board,e.auth,e.coordinator,e.registration_board,e.verifier,e.dkg,node(address(15),{}))}
    e.w3.eth.contract=lambda address,**_:nodes[address]
    e.board.functions.values.update(registry=e.reg.address,authorization=e.auth.address,domain=hex32(e.domain))
    monkeypatch.setattr(onchain_transport,"invitation",lambda w3,where:(e.registration_board,e.invite)
        if where==e.registration_board.address else (_ for _ in ()).throw(ValueError("ADDRESS_REGISTRATION_AUTHORITY")))
    monkeypatch.setattr(p,"node",lambda w3,where,kind,number:nodes[where])
    monkeypatch.setattr(p,"prove_creation",lambda *_:None)
    monkeypatch.setattr(address_mint,"configuration",lambda *_:e.ctx)
    monkeypatch.setattr(address_mint,"binding",lambda ctx,key:dict(intent=e.intent,
        script=bytes.fromhex(e.intent.derive(b'\x02'+e.reg_values['baseX'])['script_pubkey']),provenance=b'p'*32))
    e.reg_values["getRegisterOperationHash"]=lambda raws,cg,dg,deadline,seq:p.operation_hash(e.w3,e.ctx,e.auth,raws,cg,dg,deadline,seq,20)
    e.coordinator.events=SimpleNamespace(SessionCreated=lambda:SimpleNamespace(process_receipt=lambda receipt,**_:receipt["events"]))
    def send(key,method,args,**kwargs):
        e.sent.append((key,method,args,kwargs))
        if key in e.results:return e.results[key]
        if method=="createSessionFromDKG":
            sid=len(e.sessions)+1
            e.sessions[sid]=[sid,e.accounts[1].address,b"g"*32,bytes.fromhex(args[1][2:]),True,2,2,int(args[3]),True,p.ZERO,1,0,0,0,0,e.reg.address,0,0,bytes(32),0]
            result=dict(blockNumber=20,blockHash=b"h"*32,events=[dict(args=dict(sessionId=sid))])
        elif method=="publishIntent":e.public[int(args[0])]=bytes.fromhex(args[1][2:]);result={}
        elif method=="authorizeOperation":e.approved.add(bytes.fromhex(args[1][2:]));result={}
        elif method=="registerBatch":
            for raw in args[0]:
                raw=bytes.fromhex(raw[2:]);e.done[e.intent.decode(raw).intent_id]=raw
            e.view.sequence+=1;result={}
        else:raise AssertionError(method)
        e.results[key]=result;return result
    e.send=send;monkeypatch.setattr(e.service,"send",send)
    result=e.requester.request(e.intent.encode(),e.accounts[1].address);e.publish(e.requester);e.service.service_once()
    e.message=result["message_id"]
    e.driver=Driver(e.service,e.registration_board.address)
    return e


def prepared(e,deadline="1600"):
    return e.driver.prepare([e.intent.intent_id.hex()],deadline)["batch_id"]


def proposed(e):
    identifier=prepared(e);result=e.driver.propose(identifier)
    return identifier,result["intent"]


def custody_profile(e):
    """Two actual key members and a separate invitation representative."""
    custodians=list(e.invite["participants"])
    e.invite.update(participants=[address(99),*custodians],threshold="3",
        bitcoin_custody=dict(version="1",mode="custodians",threshold="2",sources=[]))
    e.registration_board.functions.values.update(operator=address(99),canPublishIntent=lambda who:who in custodians,
        intentCoordinator=e.coordinator.address)
    return custodians


@pytest.mark.parametrize("attack",[None,"auth_roster","auth_threshold","bundle_roster","creator","coordinator"])
def test_custodian_epoch_registration_and_session_review(registered_env,attack):
    e=registered_env;custody_profile(e)
    identifier,public=proposed(e)
    assert e.registration_board.functions.operator().call()!=e.service.account.address
    assert p.review(e.w3,public,e.bundle,20)["session"]==1
    assert not e.approved and not e.done  # Publication does not authorize or register.
    if attack=="auth_roster":e.auth.functions.values["getParticipants"]=e.invite["participants"]
    elif attack=="auth_threshold":e.auth.functions.values["threshold"]=3
    elif attack=="bundle_roster":
        e.bundle["participants"]=e.invite["participants"]
        e.bundle["transport_keys"]=["77"*32,*e.bundle["transport_keys"]]
    elif attack=="creator":e.sessions[1][1]=address(100)
    elif attack=="coordinator":e.registration_board.functions.values["intentCoordinator"]=address(100)
    if attack:
        with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_AUTHORITY|ADDRESS_REGISTRATION_ENROLLMENT|ADDRESS_REGISTRATION_SESSION_MISMATCH"):
            p.review(e.w3,public,e.bundle,20)


def test_v2_public_opening_authenticates_exact_intent_without_transport_secret(registered_env):
    e=registered_env;identifier=prepared(e);export=e.driver.export(identifier);proof=export["requests"][0]
    assert p.request_proof(e.w3,e.ctx,e.intent.encode(),proof,20)
    e.service.keys.clear()  # Public review needs neither HPKE secret nor private signer package.
    assert p.request_proof(e.w3,e.ctx,e.intent.encode(),proof,20)
    for changed in (dict(proof,opening="00"*32),dict(proof,message_id="00"*32)):
        with pytest.raises(ValueError,match="ADDRESS_REQUEST_ATTESTATION_REQUIRED"):
            p.request_proof(e.w3,e.ctx,e.intent.encode(),changed,20)
    raw=replace(e.intent,recipient=b"x"*20).encode()
    with pytest.raises(ValueError,match="ADDRESS_REQUEST_ATTESTATION_REQUIRED"):p.request_proof(e.w3,e.ctx,raw,proof,20)
    e.view.timestamp=1601
    assert p.request_proof(e.w3,e.ctx,e.intent.encode(),proof,20)  # Expired delivery preserves funding rights.


def test_prepare_propose_restart_and_exact_binding_completion(registered_env,monkeypatch):
    e=registered_env;identifier,public=proposed(e)
    assert p.review(e.w3,public,e.bundle,20)["operation_hash"].hex()==public["operation_hash"]
    restarted=Driver(e.service,e.registration_board.address)
    assert restarted.propose(identifier)["session"]=="1"
    assert [x[1] for x in e.sent]==["createSessionFromDKG","publishIntent"]
    e.sessions[1][10]=2
    assert restarted.execute(identifier)["status"]=="REGISTERED"
    sent=len(e.sent);e.view.cg+=1;e.view.custody=2;e.view.timestamp=2000;e.packets.clear()
    assert restarted.execute(identifier)["status"]=="REGISTERED" and len(e.sent)==sent
    assert restarted.retry(identifier,"2100")["status"]=="REGISTERED"
    assert restarted.status(identifier)["ready"] is False


@pytest.mark.parametrize("change,code",[("recipient","ADDRESS_REGISTRATION_HASH_MISMATCH"),
    ("creator","ADDRESS_REGISTRATION_SESSION_MISMATCH"),("deadline","ADDRESS_REGISTRATION_DEADLINE_CHANGED"),
    ("publication","ADDRESS_REGISTRATION_PUBLIC_INTENT_REQUIRED"),("epoch","ADDRESS_REGISTRATION_SESSION_MISMATCH")])
def test_signers_reject_mutable_or_foreign_registration(registered_env,change,code):
    e=registered_env;_,original=proposed(e);public=deepcopy(original)
    if change=="recipient":public["hash_args"][3]="1700"
    elif change=="creator":e.sessions[1][1]=e.accounts[0].address
    elif change=="deadline":e.sessions[1][7]=1700
    elif change=="publication":e.public[1]=b"{}"
    elif change=="epoch":e.coordinator.functions.values["sessionDKGResult"]=b"x"*32
    with pytest.raises(ValueError,match=code):p.review(e.w3,public,e.bundle,20)


def test_closed_generation_fences_approved_operation_and_explicit_retry_preserves_history(registered_env):
    e=registered_env;identifier,old=proposed(e);e.sessions[1][10]=2;e.approved.add(hex32(old["operation_hash"]))
    e.view.cg+=2
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_FENCED"):e.driver.execute(identifier)
    assert len(e.sent)==2
    newer=e.driver.retry(identifier)
    assert newer["attempt"]=="2" and newer["session"] is None
    record,_=e.driver._get(identifier)
    assert record["attempts"][0]["intent"]==old and len(record["attempts"])==2
    e.driver.propose(identifier);e.sessions[2][10]=2
    assert e.driver.execute(identifier)["status"]=="REGISTERED"
    assert hex32(old["operation_hash"]) in e.approved


def test_registration_expiry_requires_explicit_new_window_not_new_economic_intent(registered_env):
    e=registered_env;identifier,public=proposed(e);e.sessions[1][10]=2;e.view.timestamp=1601
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_EXPIRED"):e.driver.execute(identifier)
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_EXPIRED"):e.driver.retry(identifier)
    new=e.driver.retry(identifier,"2000");assert new["deadline"]=="2000"
    record,raws=e.driver._get(identifier)
    assert raws==[e.intent.encode()] and record["attempts"][0]["deadline"]=="1600"


def test_pause_racing_authorization_blocks_registration(registered_env,monkeypatch):
    e=registered_env;identifier,_=proposed(e);e.sessions[1][10]=2
    def raced(key,method,args,**kwargs):
        result=e.send(key,method,args,**kwargs)
        if method=="authorizeOperation":e.view.cg+=1;e.view.custody=2
        return result
    monkeypatch.setattr(e.service,"send",raced)
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_FENCED"):e.driver.execute(identifier)
    assert "registerBatch" not in [x[1] for x in e.sent]


def test_retry_requires_old_operation_unexecutable_and_is_bounded(registered_env):
    e=registered_env;identifier,_=proposed(e)
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_PENDING_ATTEMPT"):e.driver.retry(identifier,"2000")
    e.sessions[1][10]=3
    assert e.driver.retry(identifier)["attempt"]=="2"
    for ordinal in range(3,9):
        e.view.sequence+=1;assert e.driver.retry(identifier)["attempt"]==str(ordinal)
    e.view.sequence+=1
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_ATTEMPT_LIMIT"):e.driver.retry(identifier)


def test_lost_local_session_receipt_uses_same_transaction_key(registered_env,monkeypatch):
    e=registered_env;identifier=prepared(e);put=e.service.store.put
    def crash(key,value):raise RuntimeError("DISK_CRASH")
    monkeypatch.setattr(e.service.store,"put",crash)
    with pytest.raises(RuntimeError,match="DISK_CRASH"):e.driver.propose(identifier)
    assert len(e.sessions)==1
    monkeypatch.setattr(e.service.store,"put",put)
    assert e.driver.propose(identifier)["session"]=="1" and len(e.sessions)==1
    assert e.sent[0][0]==e.sent[1][0]


def test_unqualified_candidate_never_creates_paid_session(registered_env):
    e=registered_env;e.view.qualified=False
    with pytest.raises(ValueError,match="BTC_ADDRESS_VERIFIER_UNQUALIFIED"):prepared(e)
    assert not e.sent


def test_safe_export_import_and_adoption_exclude_nonce_state(registered_env,tmp_path,monkeypatch):
    e=registered_env;identifier,public=proposed(e);export=e.driver.export(identifier)
    assert set(export)=={"version","registry","pool","registration_board","intents","requests"}
    c=dict(e.configs[0],directory=str(tmp_path/"other-watch"))
    other=address_transport.Client(c,e.w3);other_driver=Driver(other,e.registration_board.address)
    result=other_driver.prepare(None,"1600",exported=export)
    assert result["batch_id"]==identifier and other.store.intents()[0][2]==0
    assert other_driver.adopt("1")["intent"]==public
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_OPERATOR_REQUIRED"):other_driver.propose(identifier)
    assert len(e.sent)==2
    assert other.store.get("nonce") is None
    bad=dict(export,secret="x")
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_INTENT_REQUIRED"):other_driver.prepare(None,"1600",exported=bad)


def test_adoption_does_not_reset_public_attempt_limit(registered_env,tmp_path):
    e=registered_env;identifier=prepared(e)
    for _ in range(7):e.view.sequence+=1;e.driver.retry(identifier)
    e.driver.propose(identifier)
    c=dict(e.configs[0],directory=str(tmp_path/"adopter"))
    other=Driver(address_transport.Client(c,e.w3),e.registration_board.address)
    assert other.adopt("1")["attempt"]=="8"
    e.view.sequence+=1
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_ATTEMPT_LIMIT"):other.retry(identifier)


def test_adoption_rejects_validated_public_operation_for_other_pool_before_persistence(registered_env,tmp_path,monkeypatch):
    e=registered_env;identifier,_=proposed(e)
    other=Driver(address_transport.Client(dict(e.configs[0],directory=str(tmp_path/"adopter")),e.w3),e.registration_board.address)
    review=p.review
    def foreign(*args,**kwargs):
        result=review(*args,**kwargs)
        return dict(result,context=dict(result["context"],pool=SimpleNamespace(address=address(100))))
    monkeypatch.setattr(p,"review",foreign)
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_AUTHORITY"):other.adopt("1")
    assert other.client.store.get(other._key(identifier)) is None


def test_private_guard_requires_bound_public_context_and_checks_both_snapshots(registered_env,monkeypatch):
    e=registered_env;_,public=proposed(e)
    monkeypatch.setattr(p,"checked_runtime",lambda *_:None)
    client=SimpleNamespace(w3=e.w3,address=e.coordinator.address,check_read_snapshot=lambda:None)
    session=SimpleNamespace(origin_contract=e.reg.address,message_hash=hex32(public["operation_hash"]),session_id=1)
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_INTENT_REQUIRED"):p.guard_signing_session(client,session)
    client.address_registration_context=(public,e.bundle)
    p.guard_signing_session(client,session)
    client.address=address(100)
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_AUTHORITY"):p.guard_signing_session(client,session)
    client.address=e.coordinator.address;e.view.dg+=1
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_FENCED"):p.guard_signing_session(client,session)


def test_family_binding_uses_its_typed_guard_and_cannot_bypass_registration(registered_env,monkeypatch):
    e=registered_env
    monkeypatch.setattr(p,'checked_runtime',lambda *_:None)
    calls=[]
    monkeypatch.setattr('signer_agent.family_registry.signing_guard',lambda *args:calls.append(args))
    session=SimpleNamespace(origin_contract=e.reg.address)
    intent=dict(artifact='BitcoinAddressRegistry',hash_method='getBindFamilyRegistryOperationHash',target=e.reg.address)
    client=SimpleNamespace(w3=e.w3,family_registry_context=(intent,e.bundle))
    p.guard_signing_session(client,session)
    assert len(calls)==1
    intent['hash_method']='getRegisterOperationHash'
    with pytest.raises(ValueError,match='FAMILY_ROOT_INTENT'):p.guard_signing_session(client,session)


def test_public_record_reorg_requires_explicit_recovery(registered_env):
    e=registered_env;identifier,_=proposed(e);e.view.reorg=True
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_RECOVERY_REQUIRED|CONTINUITY_REANCHOR_REQUIRED"):e.driver.propose(identifier)


def test_request_source_is_required_before_registration_even_for_enrolled_operator(registered_env):
    e=registered_env;e.packets.clear()
    with pytest.raises(ValueError,match="ADDRESS_REQUEST_ATTESTATION_REQUIRED"):prepared(e)
    assert not e.sent


def test_frozen_solidity_python_operation_and_request_vector():
    v=json.loads((Path(__file__).resolve().parents[2]/"web3/test/fixtures/address-registration-operations-v1.json").read_text())
    raw=[bytes.fromhex(x) for x in v["intents"]]
    reg=SimpleNamespace(address=v["registry"],functions=Functions(dict(graphId=hex32(v["graph_id"]),derivationScope=hex32(v["derivation_scope"]))))
    w3=SimpleNamespace(eth=SimpleNamespace(chain_id=int(v["chain_id"])))
    assert p.operation_hash(w3,dict(registry=reg),SimpleNamespace(address=v["authorization"]),raw,
        int(v["custody_generation"]),int(v["derivation_generation"]),int(v["deadline"]),int(v["sequence"]),0).hex()==v["operation_hash"]
    assert p.batch_id(v["registry"],raw)==v["batch_id"]
    r=v["request"]
    assert p.request_commitment(r["domain"],r["intent_id"],r["opening"],r["requester"],r["service"])==r["request_id"]


@pytest.mark.parametrize("cached",[False,True])
def test_signing_service_registration_guard_precedes_fresh_and_cached_commitments(tmp_path,monkeypatch,cached):
    from signer_agent.service import SigningService
    from signer_agent.secure_state import SecureState
    service=object.__new__(SigningService)
    service.state=SecureState(str(tmp_path/"signer.enc"),"01"*32)
    sender=address(100);session=SimpleNamespace(session_id=1,threshold=2,message_hash=b"m"*32)
    def blocked(*_):raise ValueError("ADDRESS_REGISTRATION_FENCED")
    def forbidden(*_):raise AssertionError("private nonce creation/publication forbidden")
    service.client=SimpleNamespace(chain_id=31337,address=address(1),sender=sender,
        guard_address_registration_session=blocked,submit_nonce_commitments=forbidden)
    service._active_committee=lambda *_:[sender]
    key=SecureState.session_key(31337,address(1),1)
    stored=dict(status="nonce-created",message_hash=session.message_hash.hex(),hiding="0x1",binding="0x2")
    if cached:service.state.put(key,stored)
    monkeypatch.setattr("signer_agent.service.random_nonce_pair",forbidden)
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_FENCED"):service._commit(session,[sender],{})
    assert service.state.get(key)==(stored if cached else None)


def test_signing_service_retransmission_keeps_spent_journal_when_registration_fenced(tmp_path):
    from signer_agent.service import SigningService
    from signer_agent.secure_state import SecureState
    from signer_agent.frost_math import NoncePair,build_transcript
    service=object.__new__(SigningService);service.state=SecureState(str(tmp_path/"signer.enc"),"01"*32)
    participants=[address(100),address(101)];nonces=[NoncePair(1,2),NoncePair(3,4)]
    commitments={a:(n.hiding_point,n.binding_point) for a,n in zip(participants,nonces)}
    session=SimpleNamespace(session_id=1,threshold=2,message_hash=b"m"*32,group_pubkey=b"g"*32)
    def blocked(*_):raise ValueError("ADDRESS_REGISTRATION_FENCED")
    def forbidden(*_):raise AssertionError("cached share publication forbidden")
    service.client=SimpleNamespace(chain_id=31337,address=address(1),sender=participants[0],
        guard_address_registration_session=blocked,submit_signature_share=forbidden)
    key=SecureState.session_key(31337,address(1),1)
    service.state.put(key,dict(status="nonce-created",message_hash=session.message_hash.hex(),hiding="0x1",binding="0x2"))
    transcript=build_transcript(31337,address(1),1,session.group_pubkey,session.message_hash,participants,commitments)
    service.state.prepare_share(key,transcript.hex(),123)
    stored=service.state.get(key)
    with pytest.raises(ValueError,match="ADDRESS_REGISTRATION_FENCED"):service._submit_share(session,participants,commitments)
    assert service.state.get(key)==stored
    service.state.assert_spent(key,transcript.hex())

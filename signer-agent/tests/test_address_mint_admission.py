"""Typed admission/history guards; EVM/Core/provenance are explicit fixtures."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_mint_admission import env  # existing real SecureState/history fixture
from test_address_mint import Functions, address
from signer_agent import address_mint as m, mint_admission as admission, mint_admission_client as sdk
from signer_agent import bitcoin_trust, bitcoin_onchain, bitcoin_onchain_payment


@pytest.mark.parametrize("mode",["address","custody"])
def test_frozen_typed_operations_match_solidity_vectors(mode):
    import json
    from pathlib import Path
    from eth_abi import encode
    from eth_utils import keccak
    from test_address_mint import Functions
    v=json.loads((Path(__file__).resolve().parents[2]/"web3/test/fixtures/address-mint-operations-v1.json").read_text())
    claim=[bytes.fromhex(x.removeprefix("0x")) if i in (0,1,2,8) else x for i,x in enumerate(v["claim"])]
    def hash_operation(target,selector,params,nonce):
        assert "0x"+params.hex()==v[mode]["params_hash"] and "0x"+selector.hex()==v[mode]["selector"]
        return keccak(encode(["string","uint256","address","address","bytes4","bytes32","uint256"],
            ["SATOHSI_FROST_AUTHORIZATION_V1",v["chain_id"],v["authorization"],target,selector,params,nonce]))
    context=dict(pool=SimpleNamespace(address=v["pool"]),anchor=SimpleNamespace(number=1),
        authorization=SimpleNamespace(functions=Functions(dict(hashOperation=hash_operation))),
        address_context=dict(registry=SimpleNamespace(address=v["registry"],
            functions=Functions(dict(custodyScope=bytes.fromhex(v["custody_scope"].removeprefix("0x")))))))
    args=[claim] if mode=="address" else [claim[2],claim[3],[claim[4]],[claim[5]*10**10],claim[6]]
    method="mintFromAddressProof" if mode=="address" else "mintFromProofV2"
    assert "0x"+admission._operation_hash(context,"MiningPoolV1",method,args,v["nonce"]).hex()==v[mode]["operation_hash"]


@pytest.fixture
def generated(env, monkeypatch):
    e = env
    claim=[b"i"*32,b"c"*32,e.arguments[0],e.arguments[1],e.arguments[2][0],1,1,2,b"p"*32]
    original=deepcopy(claim)
    view=SimpleNamespace(cg=1,dg=2,custody=0,derivation=0,core_ok=True,core_reviews=0)
    r=SimpleNamespace(address=address(80),functions=Functions(dict(custodyScope=b"s"*32,
        custodyGeneration=lambda:view.cg,derivationGeneration=lambda:view.dg,custodyState=lambda:view.custody)))
    ac=dict(registry=r,pool=e.contracts["pool"],number=123)
    monkeypatch.setattr(m,"configuration",lambda *_:ac)
    monkeypatch.setattr(m,"binding",lambda *_:dict(binding=claim[1],recipient=claim[4],provenance=claim[8]))
    def validate(context,proposed):
        if proposed[6:8]!=[view.cg,view.dg] or view.custody not in (0,1) or view.derivation!=0:
            raise ValueError("BTC_MINT_GENERATION_CHANGED")
    monkeypatch.setattr(m,"validate_claim",validate)
    base_read=admission._read
    def read(node,n,method,*args):
        if method=="claimModes":return 2
        if method=="getAddressClaim":return original
        if method=="getAttemptAddressClaim":return claim
        if method=="attemptFences":return claim[6:8]
        if method=="getAddressMintOperationHash":return digest_for(args[0])
        return base_read(node,n,method,*args)
    monkeypatch.setattr(admission,"_read",read)
    monkeypatch.setattr(sdk,"_read",read)
    base_hash=admission._operation_hash
    def context():
        value=e.context();value["address_context"]=ac;return value
    def digest_for(c):return base_hash(context(),"MiningPoolV1","mintFromAddressProof",[c],4)
    def hash_for(ctx,kind,method,args,nonce):
        # The existing fixture's session/attempt responses call its V1 digest
        # helper; supply the fixture's new immutable operation instead.
        if method=="mintFromProof":return digest_for(claim)
        return base_hash(ctx,kind,method,args,nonce)
    monkeypatch.setattr(admission,"_operation_hash",hash_for)
    def record(**kwargs):return admission.session_record(e.w3,e.names["board"],e.view.session,123,role="conf_ceremony",**kwargs)
    return SimpleNamespace(**locals())


def test_new_typed_operation_binds_claim_and_fences(generated):
    g=generated;r=g.record(signing=True)
    assert r["generation_mode"]==2 and r["fence_valid"]
    assert r["intent"]["hash_method"]=="getAddressMintOperationHash"
    assert r["operation_hash"]==g.digest_for(g.claim)
    old=r["operation_hash"];g.claim[6]=3
    assert g.digest_for(g.claim)!=old


def test_old_approved_operation_is_not_executable_after_generation_changes(generated):
    g=generated;g.e.view.approved=True;first=g.record()
    assert sdk._approved_current(first)
    g.view.cg=2;g.view.custody=2
    historical=g.record(require_active=False)
    assert not historical["active"] and not sdk._approved_current(historical)
    with pytest.raises(ValueError):g.record(signing=True)
    g.view.cg=3;g.view.custody=0
    assert not g.record(require_active=False)["fence_valid"]


def test_retry_history_changes_only_generation_snapshot_and_preserves_spent_state(generated):
    g=generated;first=g.record();key=sdk.remember(g.e.state,g.e.registry,first)
    g.e.state.put("unrelated-spent-nonce",dict(status="consumed",transcript="unchanged"))
    g.e.view.session=8;g.e.data["board"]["slotSession"]=8;g.view.cg=3;g.claim[6]=3
    second=g.record();newkey=sdk.remember(g.e.state,g.e.registry,second)
    assert newkey!=key and g.e.state.get(key)["intent"]==first["intent"]
    assert g.e.state.get("unrelated-spent-nonce")["transcript"]=="unchanged"
    changed=deepcopy(second);changed["economic_arguments"][0][5]=2
    changed["intent"]["session"]="9"
    with pytest.raises(ValueError,match="BTC_MINT_ECONOMIC_INTENT_CHANGED"):
        sdk.remember(g.e.state,g.e.registry,changed)


def test_same_session_cannot_be_reinterpreted_at_new_generation(generated):
    g=generated;first=g.record();sdk.remember(g.e.state,g.e.registry,first)
    g.view.cg=3;g.claim[6]=3
    with pytest.raises(ValueError,match="BTC_MINT_ADMISSION_HISTORY_CONFLICT"):
        sdk.remember(g.e.state,g.e.registry,g.record())


def test_private_signer_requires_independent_core_review_and_latest_fence(generated,monkeypatch):
    g=generated
    session=SimpleNamespace(origin_contract=g.e.names["pool"],session_id=g.e.view.session,message_hash=g.digest_for(g.claim))
    client=SimpleNamespace(w3=g.e.w3,address=g.e.names["coordinator"],_read_snapshot=g.e.block(123),
        check_read_snapshot=lambda:None,bitcoin_graph=dict(usage="MINT_ONLY_V1"))
    with pytest.raises(ValueError,match="BTC_ADDRESS_SIGNING_REVIEW_REQUIRED"):
        admission.guard_signing_session(client,session)
    client.address_mint_bitcoin=object()
    def review(*_):
        g.view.core_reviews+=1
        if not g.view.core_ok:raise ValueError("BTC_MINT_OUTPOINT_UNAVAILABLE")
    monkeypatch.setattr(m,"review_record",review)
    admission.guard_signing_session(client,session);assert g.view.core_reviews==1
    g.view.core_ok=False
    with pytest.raises(ValueError,match="BTC_MINT_OUTPOINT_UNAVAILABLE"):
        admission.guard_signing_session(client,session)
    g.view.cg=2
    with pytest.raises(ValueError):admission.guard_signing_session(client,session)
    assert g.view.core_reviews==2 # Fenced records never reach private-work review.


def test_generation_change_during_core_review_is_rechecked_before_private_work(generated,monkeypatch):
    g=generated
    session=SimpleNamespace(origin_contract=g.e.names["pool"],session_id=g.e.view.session,message_hash=g.digest_for(g.claim))
    client=SimpleNamespace(w3=g.e.w3,address=g.e.names["coordinator"],_read_snapshot=g.e.block(123),
        check_read_snapshot=lambda:None,bitcoin_graph=dict(usage="MINT_ONLY_V1"),address_mint_bitcoin=object())
    def review(*_):g.view.dg+=1
    monkeypatch.setattr(m,"review_record",review)
    with pytest.raises(ValueError):admission.guard_signing_session(client,session)


@pytest.fixture
def driver(generated,monkeypatch):
    # This fixture replaces the complete canonical graph/funding boundary.
    # The continuity catalog and its private-work hooks have separate tests.
    monkeypatch.setattr('signer_agent.bitcoin_continuity.trust_guard',lambda *a,**kw:None)
    g=generated;e=g.e;g.ac["common"]=SimpleNamespace(address=address(81))
    graph=dict(usage="MINT_ONLY_V1",evm_dkg_id=(b"d"*32).hex(),evm_confirmations="1",contracts={
        "authorization":dict(address=e.names["auth"]),"batches":dict(address=address(82)),
        "deposits":dict(address=address(81)),"address_registry":dict(address=g.r.address)})
    manifest=SimpleNamespace(context=b"mint-only-wallet")
    e.state.put("btc-mint-registry:"+manifest.context.hex(),dict(graph=graph))
    monkeypatch.setattr(bitcoin_trust,"require_registry",lambda *args,**kwargs:dict(graph=graph))
    monkeypatch.setattr(bitcoin_trust,"confirmed_block",lambda *_:e.block(123))
    monkeypatch.setattr(bitcoin_trust,"check_bitcoin_graph",lambda *_:None)
    monkeypatch.setattr(bitcoin_onchain_payment,"_auth_board",lambda *_:(None,"conf_ceremony"))
    monkeypatch.setattr(bitcoin_onchain,"snapshot_check",lambda *_:None)
    original_read=m.read
    def read(node,n,method,*args):
        if method=="deposits":return address(81)
        if node.address==e.names["board"]:return admission._read(node,n,method,*args)
        return original_read(node,n,method,*args)
    monkeypatch.setattr(m,"read",read)
    monkeypatch.setattr(m,"receipt_claim",lambda *_:list(g.claim))
    monkeypatch.setattr(m,"complete",lambda *_:e.view.complete)
    work=SimpleNamespace(core_calls=0,race=False)
    def bitcoin(*_):work.core_calls+=1;return object()
    monkeypatch.setattr(bitcoin_onchain_payment,"_bitcoin",bitcoin)
    def funding(*args,**kwargs):
        c=list(g.claim);c[6:8]=[g.view.cg,g.view.dg]
        return c,[b"raw",b"block",[],0]
    monkeypatch.setattr(m,"funding",funding)
    e.data["coordinator"]["getAuthorizationResult"]=[b"group",g.digest_for(g.claim),True,address(0),e.names["pool"],2,2,b"signature"]
    def send(registry,target,kind,method,args,**kwargs):
        e.view.sends.append((method,deepcopy(args)))
        if method in ("openAddressMint","retryGenerationMint"):
            e.view.session+=1;e.view.absent=False;e.view.state=1
            e.data["board"]["slotSession"]=e.view.session
            if method=="openAddressMint":g.claim[:]=list(args[0])
            if method=="retryGenerationMint":g.claim[6:8]=list(args[-2:])
        elif method=="authorizeOperation":
            e.view.approved=True
            if work.race:g.view.cg+=1
        elif method=="mintFromAddressProof":e.view.complete=True
        else:raise AssertionError(method)
        return dict(blockNumber=123)
    monkeypatch.setattr(bitcoin_onchain,"send",send)
    def dispatch(action="btc-address-mint-propose",**kwargs):
        args=dict(action=action,mint_admission=e.names["board"],txid=g.claim[2][::-1].hex(),vout=0,
            intent_id=g.claim[0].hex(),deadline="1000",zk_worker_config=None,zk_transport_config=None)
        args.update(kwargs)
        return m.dispatch(SimpleNamespace(**args),manifest,None,e.registry,e.state)
    return SimpleNamespace(**locals())


def test_address_client_opens_once_then_adopts_exact_canonical_claim(driver):
    d=driver;d.e.view.absent=True
    assert d.dispatch()["status"]=="AWAITING_PARTICIPANTS"
    assert d.dispatch()["status"]=="AWAITING_PARTICIPANTS"
    assert [x[0] for x in d.e.view.sends]==["openAddressMint"]


def test_address_client_executes_finalized_claim_without_mutating_deadline(driver):
    d=driver;d.e.view.state=2
    assert d.dispatch("btc-address-mint-execute")["status"]=="BTC_FUNDING_EXECUTED"
    assert [x[0] for x in d.e.view.sends]==["authorizeOperation","mintFromAddressProof"]
    assert d.e.view.sends[-1][1][-2]==d.g.claim


def test_lost_receipt_recovery_precedes_core_and_optional_worker_policy(driver):
    d=driver;d.e.view.complete=True;d.g.view.custody=3
    assert d.dispatch("btc-address-mint-execute",zk_worker_config="unused")["status"]=="BTC_FUNDING_EXECUTED"
    assert d.work.core_calls==0 and d.e.view.sends==[]


def test_old_approval_cannot_execute_after_resume_but_explicit_retry_can_rebind_generations(driver):
    d=driver;d.e.view.approved=True;d.g.view.cg=3
    with pytest.raises(ValueError,match="BTC_MINT_ADMISSION_NOT_ACTIVE"):
        d.dispatch("btc-address-mint-execute")
    assert d.e.view.sends==[]
    assert d.dispatch("btc-address-mint-retry")["status"]=="AWAITING_PARTICIPANTS"
    assert d.e.view.sends[0][0]=="retryGenerationMint" and d.e.view.sends[0][1][-2:]==[3,2]


def test_generation_race_after_authorization_prevents_monetary_send(driver):
    d=driver;d.e.view.state=2;d.work.race=True
    with pytest.raises(ValueError,match="BTC_MINT_ADMISSION_NOT_ACTIVE"):
        d.dispatch("btc-address-mint-execute")
    assert [x[0] for x in d.e.view.sends]==["authorizeOperation"]


def test_address_client_never_extends_original_deadline_or_bypasses_worker_profile(driver,monkeypatch):
    with pytest.raises(ValueError,match="BTC_MINT_ECONOMIC_INTENT_CHANGED"):driver.dispatch(deadline="2000")
    def no_profile(*_):raise ValueError('BTC_ADDRESS_ZK_WORKER_PROFILE_REQUIRED')
    monkeypatch.setattr('signer_agent.zk_worker_transport.request_from_consumer',no_profile)
    with pytest.raises(ValueError,match="BTC_ADDRESS_ZK_WORKER_PROFILE_REQUIRED"):driver.dispatch(zk_transport_config="unused")
    assert driver.e.view.sends==[]


def test_address_client_requests_stable_registered_need_before_mint_admission(driver,monkeypatch):
    needs=[]
    def request(path,graph,need):needs.append(need);return dict(status='NEEDS_STATE',local_need_id='one')
    monkeypatch.setattr('signer_agent.zk_worker.request_from_consumer',request)
    assert driver.dispatch(zk_worker_config='explicit')['status']=='AWAITING_ZK_STATE'
    driver.g.view.cg+=1
    assert driver.dispatch(zk_worker_config='explicit')['status']=='AWAITING_ZK_STATE'
    assert needs[0]==needs[1] and needs[0]['kind']=='address_deposit'
    assert needs[0]['intent_id']==driver.g.claim[0].hex() and driver.e.view.sends==[]


def test_address_client_rechecks_generations_after_worker_becomes_ready(driver,monkeypatch):
    driver.e.view.absent=True
    def request(*_):driver.g.view.cg+=1;return dict(status='READY')
    monkeypatch.setattr('signer_agent.zk_worker.request_from_consumer',request)
    assert driver.dispatch(zk_worker_config='explicit')['status']=='AWAITING_PARTICIPANTS'
    assert driver.e.view.sends[0][1][0][6]==driver.g.view.cg

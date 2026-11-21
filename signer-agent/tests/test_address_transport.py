"""Real HPKE/SQLite/native descriptors; explicit EVM graph and delivery fixtures."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_utils import keccak
from eth_abi import encode
from web3.datastructures import AttributeDict

from signer_agent import crypto,address_transport as m,address_mint,spv_transport
from signer_agent.operations import canonical
from signer_agent.deposit_intent import DepositIntentV1
from test_address_mint import Functions,address

ROOT=Path(__file__).resolve().parents[2]
VECTOR=json.loads((ROOT/"rust/zkspv/derivation-qualification/public-vector.json").read_text())


def private(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    path.write_text(canonical(value));path.chmod(0o600);return str(path)


@pytest.fixture
def delivery(tmp_path,monkeypatch):
    accounts=[Account.from_key(i.to_bytes(32,"big")) for i in (1,2)]
    keys=[crypto.call("transport_key") for _ in accounts]
    view=SimpleNamespace(timestamp=1000,reorg=False,custody=0,derivation=1)
    def block(n):return AttributeDict(dict(number=n if type(n) is int else 20,hash=(b"z" if view.reorg else b"h")*32,timestamp=view.timestamp))
    blank=(bytes(32),0,0,address(0),address(0),0,0,0,b"")
    packets={};routes={};inboxes={a.address:[] for a in accounts};epochs={a.address:1 for a in accounts}
    public={(a.address,1):bytes.fromhex(k["public"]) for a,k in zip(accounts,keys)}
    template=DepositIntentV1.decode(bytes.fromhex(VECTOR["intent_bytes"]))
    reg_values=dict(bitcoinGenesis=template.bitcoin_genesis,graphId=template.custody_graph_id,dkg=address(10),
        walletId=template.wallet_id,walletResult=template.wallet_result,keyEpoch=template.key_epoch,
        servicePolicyHash=template.service_policy_hash,baseX=bytes.fromhex(VECTOR["base_group"])[1:],
        getBinding=lambda _:(b"",),custodyGeneration=1,derivationGeneration=1,validateRecipient=lambda *_:0,
        custodyState=lambda:view.custody,derivationState=lambda:view.derivation,issuanceClosed=False)
    reg_values.update(deposits=address(6),batches=address(9),authorization=address(7),escrow=address(8),
        custodyScope=b'c'*32,derivationScope=keccak(b'synthetic derivation scope'),custodyStatusNonce=0,derivationStatusNonce=0,
        PROFILE=keccak(text='BASE_POST_DKG_EVEN_Y__INTENT_TAPLEAF_V1'),verifier=address(14),verifierRuntimeHash=keccak(b''),
        verifierSourceHash=b's'*32,qualificationCommitment=b'q'*32)
    def node(where,values):return SimpleNamespace(address=where,functions=Functions(values))
    pool=node(address(5),dict(mpToken=address(3),authorization=address(7),escrow=address(8)))
    reg=node(address(4),reg_values);common=node(address(6),dict(spv=address(30),addressRegistry=address(4),
        spendRecorder=address(8),authorization=address(7)));batch=node(address(9),{})
    domain=keccak(encode(["bytes32","uint256","address","address","bytes32","address"],[
        keccak(text="SATOHSI_ADDRESS_MESSAGES_V1"),31337,address(11),reg.address,reg_values["graphId"],address(7)])).hex()
    board=node(address(11),dict(currentEpoch=lambda who:epochs[who],transportKeys=lambda who,epoch:public.get((who,epoch),bytes(32)),
        messageCount=lambda who:len(inboxes[who]),messageAt=lambda who,index:bytes.fromhex(inboxes[who][index]),
        packet=lambda key:packets.get(key.hex(),blank),route=lambda key:routes.get(key.hex(),(address(0),address(0),bytes(32))),
        serviceAllowed=lambda who:who==accounts[1].address))
    w3=SimpleNamespace(eth=SimpleNamespace(chain_id=31337,get_block=block,get_code=lambda *_,**kw:b"",
        contract=lambda address,**_: {pool.address:pool,reg.address:reg,board.address:board}[address]))
    pool.w3=w3;ctx=dict(pool=pool,registry=reg,common=common,batch=batch,number=20)
    original_context=m.Client.context
    monkeypatch.setattr(m.Client,"context",lambda self:(block("latest"),pool,ctx,board,domain))
    configs=[]
    for i,(account,key) in enumerate(zip(accounts,keys)):
        home=tmp_path/str(i)
        c=dict(version="1",chain_id="31337",evm_confirmations="1",evm_rpc="http://127.0.0.1:8545",
            bitcoin_network="regtest",bitcoin_policy=None,registry=reg.address,pool=pool.address,board=board.address,
            identity_file=private(home/"identity.json",dict(private_key=account.key.hex())),
            key_files=[private(home/"key.json",dict(epoch="1",**key))],directory=str(home/"data"),
            bitcoin_rpc=None,bitcoin_cookie=None,max_messages="128",max_messages_per_poll="16",max_intents="32",max_outputs="64",max_archive_bytes="1073741824",
            max_blocks_per_poll="2",max_reorg="100",ttl_seconds="600",max_publish_attempts="4",max_tx_gas="1000000",
            max_transaction_cost_wei="10000000",max_total_cost_wei="100000000")
        config_path=private(home/"config.json",c);assert m.load_config(config_path)==c
        configs.append(c)
    clients=[m.Client(c,w3) for c in configs]
    intent=clients[0].make_intent(address(20),"99"*32)
    def publish(client):
        with client.store.journal.db() as db:rows=db.execute("SELECT id,body FROM address_packets WHERE direction='OUT'").fetchall()
        for key,encoded in rows:
            p=json.loads(encoded);h=p["header"]
            if key in packets:continue
            if h["kind"]=="REQUEST":routes[h["route"]]=(h["sender"],h["recipient"],bytes.fromhex(p["request_id"]))
            packets[key]=(bytes.fromhex(h["route"]),m.KINDS[h["kind"]],int(h["sequence"]),h["sender"],h["recipient"],
                int(h["sender_epoch"]),int(h["recipient_epoch"]),int(h["expires_at"]),bytes.fromhex(p["ciphertext"]))
            inboxes[h["recipient"]].append(key)
    return SimpleNamespace(**locals(),requester=clients[0],service=clients[1])


def test_authenticated_encrypted_request_durable_ack_and_restart(delivery):
    e=delivery;result=e.requester.request(e.intent.encode(),e.accounts[1].address)
    e.publish(e.requester);e.service.service_once()
    assert e.service.store.intents()[0][2]==0
    e.publish(e.service);e.requester.poll();reply=e.requester.replies()
    assert reply["routes"][result["route"]]["acknowledged"] and reply["ready"] is False
    restarted=m.Client(e.configs[0],e.w3)
    assert restarted.request(e.intent.encode(),e.accounts[1].address)==result
    assert len(e.inboxes[e.accounts[1].address])==1
    assert restarted.replies()==reply
    # Calldata contains only public routing and opaque HPKE bytes.
    for p in e.packets.values():assert e.intent.encode() not in p[8]


def test_hpke_address_domain_header_and_epoch_are_not_spv_fallback(delivery):
    e=delivery;e.requester.request(e.intent.encode(),e.accounts[1].address)
    with e.requester.store.journal.db() as db:p=json.loads(db.execute("SELECT body FROM address_packets").fetchone()[0])
    assert m.open_packet(e.domain,p["header"],p["ciphertext"],e.keys[1]["secret"])["intent_id"]==e.intent.intent_id.hex()
    with pytest.raises(RuntimeError):spv_transport.open_packet(e.domain,p["header"],p["ciphertext"],e.keys[1]["secret"])
    changed=dict(p["header"],recipient_epoch="2")
    with pytest.raises(RuntimeError):m.open_packet(e.domain,changed,p["ciphertext"],e.keys[1]["secret"])


def test_legacy_v1_delivery_keeps_exact_retry_but_needs_explicit_v2_for_registration(delivery):
    e=delivery;first=e.requester.request(e.intent.encode(),e.accounts[1].address)
    with e.requester.store.journal.db() as db:
        row=db.execute("SELECT id,body FROM address_packets").fetchone();packet=json.loads(row[1])
        packet["body"].pop("commitment_salt");packet["body"]["version"]="1"
        packet["ciphertext"]=m.seal(e.domain,packet["header"],packet["body"],e.keys[1]["public"])
        db.execute("UPDATE address_packets SET body=? WHERE id=?",(canonical(packet),row[0]))
    e.publish(e.requester);e.service.service_once()
    with e.service.store.journal.db() as db:
        assert "opening" not in json.loads(db.execute("SELECT body FROM address_requests").fetchone()[0])["evidence"]
    assert e.requester.request(e.intent.encode(),e.accounts[1].address)==first
    second=e.requester.request(e.intent.encode(),e.accounts[1].address,delivery="1")
    assert second["route"]!=first["route"]
    e.publish(e.requester);e.service.service_once()
    with e.service.store.journal.db() as db:
        assert "opening" in json.loads(db.execute("SELECT body FROM address_requests ORDER BY rowid DESC").fetchone()[0])["evidence"]


def test_authenticated_but_wrong_v2_opening_is_quarantined(delivery):
    e=delivery;e.requester.request(e.intent.encode(),e.accounts[1].address)
    with e.requester.store.journal.db() as db:
        row=db.execute("SELECT id,body FROM address_packets").fetchone();packet=json.loads(row[1])
        packet["body"]["commitment_salt"]="00"*32
        packet["ciphertext"]=m.seal(e.domain,packet["header"],packet["body"],e.keys[1]["public"])
        db.execute("UPDATE address_packets SET body=? WHERE id=?",(canonical(packet),row[0]))
    e.publish(e.requester);e.service.service_once()
    assert not e.service.store.intents()
    with e.service.store.journal.db() as db:
        assert db.execute("SELECT state FROM address_packets").fetchone()[0]=="QUARANTINED"


def test_service_crash_after_descriptor_commit_resumes_one_ack(delivery,monkeypatch):
    e=delivery;e.requester.request(e.intent.encode(),e.accounts[1].address);e.publish(e.requester)
    reply=e.service._reply
    monkeypatch.setattr(e.service,"_reply",lambda *_:(_ for _ in ()).throw(RuntimeError("SIMULATED_DISK_CRASH")))
    with pytest.raises(RuntimeError,match="SIMULATED_DISK_CRASH"):e.service.service_once()
    assert len(e.service.store.intents())==1
    monkeypatch.setattr(e.service,"_reply",reply)
    e.service.service_once();e.service.service_once()
    with e.service.store.journal.db() as db:assert db.execute("SELECT COUNT(*) FROM address_packets WHERE direction='OUT'").fetchone()[0]==1


def test_status_is_generation_aware_hint_and_cannot_claim_ready(delivery):
    e=delivery;result=e.requester.request(e.intent.encode(),e.accounts[1].address);e.publish(e.requester)
    e.service.service_once();e.service.publish_status();e.publish(e.service)
    e.requester.poll();r=e.requester.replies()["routes"][result["route"]]
    assert r["status"]=="DERIVATION_QUALIFICATION_REQUIRED" and r["ready"] is False
    e.view.custody=2;e.service.publish_status();e.publish(e.service);e.requester.poll()
    assert e.requester.replies()["routes"][result["route"]]["status"]=="ISSUANCE_SUSPENDED"


def test_transport_reorg_does_not_delete_durable_watch_or_trust_old_ack(delivery):
    e=delivery;result=e.requester.request(e.intent.encode(),e.accounts[1].address);e.publish(e.requester)
    e.service.service_once();e.publish(e.service);e.requester.poll()
    ack=e.inboxes[e.accounts[0].address][0];e.packets.pop(ack)
    assert not e.requester.replies()["routes"][result["route"]]["acknowledged"]
    assert e.service.store.intents()[0][0]==e.intent.intent_id.hex()


def test_missing_old_transport_key_does_not_discard_historical_request(delivery):
    e=delivery;e.requester.request(e.intent.encode(),e.accounts[1].address);e.publish(e.requester)
    e.service.keys.clear()
    with pytest.raises(ValueError,match="ADDRESS_LOCAL_KEY_REQUIRED"):e.service.poll()
    assert e.service.store.get("inbox_cursor") is None


def test_corrupted_ciphertext_is_quarantined_without_poisoning_next_request(delivery):
    e=delivery;one=e.requester.request(e.intent.encode(),e.accounts[1].address);e.publish(e.requester)
    p=list(e.packets[one["message_id"]]);p[8]=p[8][:-1]+bytes([p[8][-1]^1]);e.packets[one["message_id"]]=tuple(p)
    e.requester.request(e.intent.encode(),e.accounts[1].address,delivery="1");e.publish(e.requester)
    e.service.service_once();assert len(e.service.store.intents())==1
    with e.service.store.journal.db() as db:assert db.execute("SELECT COUNT(*) FROM address_packets WHERE state='QUARANTINED'").fetchone()[0]==1


def test_altered_recipient_or_requester_never_rebinds_transport_identity(delivery):
    from dataclasses import replace
    e=delivery;bad=replace(e.intent,requester=b"q"*20)
    with pytest.raises(ValueError,match="ADDRESS_REQUESTER_MISMATCH"):e.requester.request(bad.encode(),e.accounts[1].address)
    with pytest.raises(ValueError,match="ADDRESS_SERVICE_ROLE"):e.requester.request(e.intent.encode(),e.accounts[0].address)


def test_message_budget_and_exact_ciphertext_are_durable(delivery):
    e=delivery;e.requester.c["max_messages"]="1"
    first=e.requester.request(e.intent.encode(),e.accounts[1].address)
    assert e.requester.request(e.intent.encode(),e.accounts[1].address)==first
    with pytest.raises(ValueError,match="ADDRESS_INBOX_FULL"):e.requester.request(e.intent.encode(),e.accounts[1].address,delivery="1")


def test_counterfeit_board_runtime_is_not_admitted(delivery,monkeypatch):
    e=delivery
    monkeypatch.setattr(address_mint,"configuration",lambda *_:e.ctx)
    with pytest.raises(ValueError,match="BTC_CONTRACT_BUILD_MISMATCH"):
        e.original_context(e.requester)


def test_request_outbox_and_idempotency_mapping_commit_atomically(delivery,monkeypatch):
    e=delivery;put=e.requester.store._put
    def fail(db,key,value):
        if key.startswith("request:"):raise RuntimeError("SIMULATED_DISK_CRASH")
        put(db,key,value)
    monkeypatch.setattr(e.requester.store,"_put",fail)
    with pytest.raises(RuntimeError,match="SIMULATED_DISK_CRASH"):e.requester.request(e.intent.encode(),e.accounts[1].address)
    with e.requester.store.journal.db() as db:assert db.execute("SELECT COUNT(*) FROM address_packets").fetchone()[0]==0
    monkeypatch.setattr(e.requester.store,"_put",put)
    e.requester.request(e.intent.encode(),e.accounts[1].address)
    with e.requester.store.journal.db() as db:assert db.execute("SELECT COUNT(*) FROM address_packets").fetchone()[0]==1


def test_authenticated_service_still_cannot_assert_ready(delivery):
    e=delivery;r=e.requester.request(e.intent.encode(),e.accounts[1].address);e.publish(e.requester)
    e.service.service_once()
    b,_,_,board,domain=e.service.context()
    e.service._reply(r["route"],"STATUS",1,dict(version="1",request_id=e.routes[r["route"]][2].hex(),
        intent_id=e.intent.intent_id.hex(),status="READY",descriptor_hash="00"*32,ready=True),b,board,domain)
    e.publish(e.service);e.requester.poll()
    assert e.requester.replies()["routes"][r["route"]]["status"]=="INVALID_REPLY"
    assert e.requester.replies()["ready"] is False


def test_public_binding_recovery_needs_no_previous_server_transport_key(delivery,monkeypatch,tmp_path):
    e=delivery;fresh=crypto.call("transport_key")
    c=dict(e.configs[1],directory=str(tmp_path/"recovered"),
        key_files=[private(tmp_path/"new-key.json",dict(epoch="2",**fresh))])
    recovered=m.Client(c,e.w3)
    monkeypatch.setattr(address_mint,"binding",lambda *_:dict(intent=e.intent,provenance=b"p"*32,
        script=bytes.fromhex(e.intent.derive(b'\x02'+e.reg_values['baseX'])['script_pubkey'])))
    r=recovered.recover_binding(e.intent.intent_id.hex())
    assert r["status"]=="ADDRESS_DESCRIPTOR_RECOVERED" and recovered.store.intents()[0][2]==0
    assert recovered.store.intents()[0][1]["intent"]==e.intent.encode().hex()


def test_sender_enforces_per_transaction_and_durable_total_gas_budget(delivery,monkeypatch):
    e=delivery
    monkeypatch.setattr(m.Bootstrap,"__init__",lambda self,profile,state:setattr(self,"profile",profile))
    monkeypatch.setattr(m.Bootstrap,"build_transaction",lambda *_:dict(gas=100,maxFeePerGas=2,value=0))
    def apply(self,*_,**kw):
        self.build_transaction(self.profile["steps"][0],None,0)
        return dict(status="VERIFIED")
    monkeypatch.setattr(m.Bootstrap,"apply_confirmed",apply)
    e.requester.c["max_total_cost_wei"]="300"
    assert e.requester.send("same","registerKey",["1","0x"+e.keys[0]["public"]])["status"]=="VERIFIED"
    assert e.requester.send("same","registerKey",["1","0x"+e.keys[0]["public"]])["status"]=="VERIFIED"
    with pytest.raises(ValueError,match="ADDRESS_GAS_BUDGET"):
        e.requester.send("other","registerKey",["2","0x"+e.keys[0]["public"]])
    # A new watch home or restored descriptor archive cannot reset gas usage.
    fresh=m.Client(dict(e.configs[0],directory=str(Path(e.configs[0]["directory"]).parent/"fresh-watch")),e.w3)
    with pytest.raises(ValueError,match="ADDRESS_GAS_BUDGET"):
        fresh.send("third","registerKey",["2","0x"+e.keys[0]["public"]])


def test_expired_new_request_is_not_acknowledged_but_already_durable_watch_survives(delivery):
    e=delivery;r=e.requester.request(e.intent.encode(),e.accounts[1].address);e.publish(e.requester)
    e.view.timestamp=2000;e.service.service_once()
    assert e.service.store.intents()==[]
    assert e.service.store.get("rejected:"+r["message_id"])["reason"]=="ADDRESS_REQUEST_EXPIRED"


def test_status_retry_keeps_queued_ciphertext_when_projection_cursor_was_lost(delivery):
    e=delivery;r=e.requester.request(e.intent.encode(),e.accounts[1].address);e.publish(e.requester)
    e.service.service_once();e.service.publish_status()
    with e.service.store.journal.db() as db:
        original=db.execute("SELECT body FROM address_packets WHERE id=?",(m.message_key(r["route"],"STATUS","1"),)).fetchone()[0]
        db.execute("DELETE FROM address_meta WHERE id=?",("status:"+r["route"],))
    e.view.custody=2;e.service.publish_status();e.service.publish_status()
    with e.service.store.journal.db() as db:
        assert db.execute("SELECT body FROM address_packets WHERE id=?",(m.message_key(r["route"],"STATUS","1"),)).fetchone()[0]==original
        assert db.execute("SELECT body FROM address_packets WHERE id=?",(m.message_key(r["route"],"STATUS","2"),)).fetchone() is not None


def test_missing_gas_ledger_cannot_silently_reset_authorized_budget(delivery):
    e=delivery;e.requester.costs.path.unlink()
    with pytest.raises(ValueError,match="ADDRESS_GAS_RECOVERY_REQUIRED"):m.Client(e.configs[0],e.w3)

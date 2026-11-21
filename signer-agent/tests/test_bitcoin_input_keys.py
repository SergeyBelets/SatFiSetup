"""Real native DKG/mixed-input signatures and SecureState; explicit Core/EVM fixtures."""
import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from coincurve import PublicKeyXOnly
from eth_abi import encode
from eth_utils import keccak
from web3.datastructures import AttributeDict

from signer_agent import crypto, address_mint, bitcoin_input_keys as m, bitcoin_worker as worker
from signer_agent import bitcoin_trust as trust, bitcoin_onchain as transport, bitcoin_onchain_payment as payment
from signer_agent.deposit_intent import DepositIntentV1, PROFILE
from signer_agent.dkg_protocol import Manifest
from signer_agent.secure_state import SecureState
from signer_agent.bitcoin_onchain_plan import validate_plan, serialize_plan, decode_plan, onchain_plan_hash, DOMAIN
from signer_agent.bitcoin_proofs import sha256d
from test_address_mint import Functions, address

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def native_keys():
    first={str(i):crypto.call("part1",index=i,n=3,t=2) for i in (1,2,3)}
    second={i:crypto.call("part2",secret=v["secret"],round1={j:w["package"] for j,w in first.items() if j!=i}) for i,v in first.items()}
    return [crypto.call("part3",secret=second[i]["secret"],
        round1={j:v["package"] for j,v in first.items() if j!=i},
        round2={j:v["packages"]["%064x"%int(i)] for j,v in second.items() if j!=i}) for i in first]


@pytest.fixture
def signing(tmp_path,monkeypatch,native_keys):
    raw=bytes.fromhex(json.loads((ROOT/"rust/zkspv/derivation-qualification/public-vector.json").read_text())["intent_bytes"])
    intent=DepositIntentV1.decode(raw); roster=tuple(address(i) for i in (1,2,3))
    manifest=Manifest(31337,address(10),b"w"*32,9,"BTC_SPEND",roster,(b"a"*32,b"b"*32,b"c"*32),2)
    intent=replace(intent,evm_chain_id=31337,deposit_registry=bytes.fromhex(address(11)[2:]),
        pool=bytes.fromhex(address(12)[2:]),mp_token=bytes.fromhex(address(13)[2:]),dkg_registry=bytes.fromhex(address(10)[2:]),
        wallet_id=manifest.session_id,wallet_result=b"r"*32,custody_graph_id=b"g"*32)
    key=dict(index=1,participant=roster[0],result_hash=(b"r"*32).hex(),result=native_keys[0])
    runtime=b"explicit test verifier runtime";runtime_hash=keccak(runtime)
    v=SimpleNamespace(cg=1,dg=2,custody=0,derivation=0,qualified=True,core_calls=[],spent=False,
        confirmations=6,coinbase=False,backing=True,change=False,begin=0,reorg=False,bound=True)
    def node(where,values):return SimpleNamespace(address=where,functions=Functions(values))
    rows={}; scripts={}; pairs=[]
    for i in (0,1):
        variant=replace(intent,request_nonce=bytes([i+1])*32)
        d=variant.derive(bytes.fromhex(key["result"]["group"]))
        desc=[bytes.fromhex(d[x]) for x in ("intent_id","merkle_root","tweak","output_key")]
        desc += [d["output_parity_odd"],bytes.fromhex(d["binding_commitment"]),bytes.fromhex(d["wallet_context"])]
        prov=keccak(encode(["bytes32"]*10+["uint256","bytes32"],[keccak(text="SATOHSI_ADDRESS_VALIDATION_V1"),
            b"s"*32,keccak(text=PROFILE),runtime_hash,b"o"*32,b"q"*32,intent.wallet_result,desc[6],variant.intent_id,desc[5],2,b"h"*32]))
        rows[variant.intent_id]=[variant.encode(),address(12),address(13),address_mint.Web3.to_checksum_address(variant.requester),
            address_mint.Web3.to_checksum_address(variant.recipient),keccak(bytes.fromhex(d["script_pubkey"])),variant.request_nonce,desc,prov,2,b"h"*32]
        scripts[keccak(bytes.fromhex(d["script_pubkey"]))]=variant.intent_id
        pairs.append((d,prov))
    base="5120"+key["result"]["group"][2:]
    common_values=dict(addressRegistry=address(11),scriptPools=lambda h:address(12) if h in scripts or h==keccak(bytes.fromhex(base)) else address(0),
        addressIntents=lambda h:scripts.get(h,bytes(32)),backingSats=lambda _:9000 if v.backing else 0,
        walletOutput=lambda _:v.change)
    common=node(address(14),common_values)
    reg_values=dict(walletId=manifest.session_id,walletResult=intent.wallet_result,baseX=bytes.fromhex(key["result"]["group"])[1:],
        dkg=manifest.registry,deposits=common.address,bitcoinGenesis=intent.bitcoin_genesis,graphId=intent.custody_graph_id,
        keyEpoch=intent.key_epoch,custodyGeneration=lambda:v.cg,derivationGeneration=lambda:v.dg,custodyState=lambda:v.custody,
        derivationState=lambda:v.derivation,derivationScope=b"s"*32,verifierRuntimeHash=runtime_hash,verifierSourceHash=b"o"*32,
        qualificationCommitment=b"q"*32,servicePolicyHash=intent.service_policy_hash,verifier=address(15),getBinding=lambda i:rows[i])
    reg=node(address(11),reg_values);verifier=node(address(15),dict(QUALIFIED=lambda:v.qualified,PROFILE=keccak(text=PROFILE)))
    pool=node(address(12),dict(mpToken=address(13)))
    plan=dict(version="3",network="regtest",batch_registry=address(16),batch_id="ab"*32,signing_board=address(17),
        wallet_result=key["result_hash"],wallet_script=base,deadline="200",fee="1000",max_fee="2000",
        inputs=[dict(outpoint=("11"*32)+":0",amount="10000",script=base)]+[
            dict(outpoint=(str(i+2)*64)+":0",amount="10000",script=d["script_pubkey"]) for i,(d,_) in enumerate(pairs)],
        outputs=[dict(amount="5000",script="0014"+"55"*20),dict(amount="24000",script=base)],
        withdrawal_refs=["66"*32],signers=["1","2"],address_registry=reg.address,custody_generation="1",derivation_generation="2",
        input_keys=[intent.base_input_key(bytes.fromhex(key["result"]["group"]))]+[d["input_key"] for d,_ in pairs],
        input_provenance=["00"*32]+[p.hex() for _,p in pairs])
    block=AttributeDict(dict(number=10,hash=b"h"*32,timestamp=100))
    def get_block(n):return AttributeDict(dict(number=n if isinstance(n,int) else 10,hash=(b"z" if v.reorg else b"h")*32,timestamp=100))
    batch_values=dict(feeReserve=address(0),dkg=manifest.registry,walletId=manifest.session_id,
        batches=lambda _:(onchain_plan_hash(manifest,plan),intent.wallet_result,200,2),reservationRoot=lambda _:b"x"*32,
        replacementOf=lambda _:bytes(32),reservations=lambda _:b"x"*32,withdrawalEscrow=address(18),batchState=lambda _:0)
    batches=node(address(16),batch_values)
    escrow=node(address(18),dict(payment=lambda _:(sha256d(bytes.fromhex(m.build(plan)["unsigned"])),0,False,[bytes.fromhex("66"*32)]),
            requests=lambda _:[address(12),address(1),address(13),0,5000*10**10,bytes.fromhex("0014"+"55"*20),b"x"*32 if v.bound else bytes(32),2 if v.bound else 1],
            depositRegistry=common.address,minP2WPKHSats=294,minP2TRSats=330))
    nodes={n.address:n for n in (common,reg,verifier,pool,batches,escrow)}
    w3=SimpleNamespace(eth=SimpleNamespace(chain_id=31337,contract=lambda address,**_:nodes[address],get_block=get_block,get_code=lambda *_,**kw:runtime))
    pool.w3=w3
    registry=SimpleNamespace(w3=w3)
    graph=dict(usage=m.PROFILE,contracts={k:dict(address=n.address) for k,n in (("address_registry",reg),("deposits",common),("batches",batches))})
    registry.bitcoin_graph=graph
    ac=dict(pool=pool,registry=reg,common=common,number=10)
    # Exact runtime/constructor admission is covered by trust/provenance tests.
    # This fixture executes immutable binding/provenance and curve checks for real.
    monkeypatch.setattr(address_mint,"configuration",lambda *_:ac)
    monkeypatch.setattr(trust,"require_registry",lambda *_,**kw:dict(graph=graph))
    monkeypatch.setattr(transport,"require_registry",lambda *_,**kw:dict(graph=graph))
    monkeypatch.setattr("signer_agent.callback_admission.validate_callback",lambda *_:None)
    class Core:
        network="regtest"
        def check_network(self):pass
        def call(self,method,*args):
            v.core_calls.append((method,args))
            if method=="gettxout":
                if v.spent:return None
                item=next(i for i in plan["inputs"] if i["outpoint"]==args[0]+":"+str(args[1]))
                return dict(scriptPubKey=dict(hex=item["script"]),value=Decimal(item["amount"])/10**8,
                    confirmations=v.confirmations,coinbase=v.coinbase)
            if method=="getrawtransaction":return dict(txid=args[0],vout=[])
            if method=="getbestblockhash":return "77"*32
            if method=="getblockhash":return intent.bitcoin_genesis.hex()
            raise AssertionError(method)
    bitcoin=Core()
    states=[SecureState(str(tmp_path/str(i)/"btc.enc"),"test-state-password") for i in (1,2)]
    keys=[dict(key,index=i,participant=roster[i-1],result=native_keys[i-1]) for i in (1,2)]
    return SimpleNamespace(**locals())


def test_mixed_plan_roundtrip_build_and_native_signatures_share_the_existing_journal(signing):
    e=signing;p=e.plan
    assert decode_plan(serialize_plan(p),e.manifest)==p
    original=[deepcopy(k["result"]) for k in e.keys]
    commits=[worker.sign_step("btc-commit",e.manifest,k,p,e.registry,s,e.bitcoin) for k,s in zip(e.keys,e.states)]
    work=dict(p,commitments=[{str(i+1):c["values"][j] for i,c in enumerate(commits)} for j in range(3)])
    shares=[worker.sign_step("btc-sign",e.manifest,k,work,e.registry,s,e.bitcoin) for k,s in zip(e.keys,e.states)]
    built=m.build(p);signatures=[]
    for j,message in enumerate(built["sighashes"]):
        sig=crypto.call("bitcoin_aggregate_v1",public_package=e.keys[0]["result"]["public_package"],
            input_key=p["input_keys"][j],commitments=work["commitments"][j],
            shares={str(i+1):s["values"][j] for i,s in enumerate(shares)},message=message)["signature"]
        assert PublicKeyXOnly(bytes.fromhex(p["inputs"][j]["script"])[2:]).verify(bytes.fromhex(sig),bytes.fromhex(message))
        signatures.append(sig)
    signed=crypto.call("bitcoin_attach_v1",unsigned=built["unsigned"],package=dict(p,input_key_version=1),signatures=signatures)
    assert signed["txid"]==built["txid"] and signed["wtxid"]!=signed["txid"]
    assert [k["result"] for k in e.keys]==original
    for j in range(3):
        sid="btc:"+e.manifest.context.hex()+":"+p["batch_id"]+":"+str(j)
        saved=e.states[0].get(sid);assert saved["status"]=="share-prepared" and "nonces" not in saved
        e.states[0].assert_spent(sid,saved["transcript"])
    # Exact cached shares remain usable without Core/UTXO access, never new crypto.
    e.v.spent=True;e.v.core_calls.clear()
    assert transport.cached_transport_step("btc-sign",e.manifest,e.key,work,e.registry,e.states[0])["values"]==shares[0]["values"]
    assert e.v.core_calls==[]


@pytest.mark.parametrize("field,value",[("cg",2),("dg",3),("custody",2),("custody",3),("derivation",1),("derivation",2),("qualified",False)])
def test_fences_stop_private_work_and_never_reset_journal(signing,field,value):
    e=signing;setattr(e.v,field,value)
    with pytest.raises(ValueError):worker.sign_step("btc-commit",e.manifest,e.key,e.plan,e.registry,e.states[0],e.bitcoin)
    assert e.states[0].get("btc:"+e.manifest.context.hex()+":"+e.plan["batch_id"]+":0") is None


@pytest.mark.parametrize("field",["intent","binding_commitment","wallet_context","base_group","extra"])
def test_operator_cannot_supply_unbound_or_arbitrary_key_material(signing,field):
    e=signing;p=deepcopy(e.plan);d=p["input_keys"][1]
    d[field]="00"*(len(d[field])//2) if field in d else "secret-key-package"
    with pytest.raises(ValueError):validate_plan(p,e.manifest)


def test_generation_snapshot_and_provenance_are_part_of_reserved_plan_hash(signing):
    e=signing;old=onchain_plan_hash(e.manifest,e.plan)
    for field in ("custody_generation","derivation_generation"):
        changed=dict(e.plan,**{field:"3"})
        assert onchain_plan_hash(e.manifest,changed)!=old
    changed=deepcopy(e.plan);changed["input_provenance"][1]="99"*32
    assert onchain_plan_hash(e.manifest,changed)!=old
    with pytest.raises(ValueError,match="BTC_INPUT_KEY_BINDING"):m.review(e.manifest,e.key,changed,e.registry,e.graph,10)


def test_unminted_address_is_protected_without_smp_marker(signing):
    e=signing;e.v.backing=False
    with pytest.raises(ValueError,match="BTC_UNMINTED_COMMITTED_DEPOSIT"):
        payment.protect_unminted_deposit(e.common,e.plan["inputs"][1],e.bitcoin,10,address_mode=True)
    assert not e.v.core_calls
    e.v.change=True
    payment.protect_unminted_deposit(e.common,e.plan["inputs"][1],e.bitcoin,10,address_mode=True)


def test_changed_binding_is_never_trusted_from_a_cached_descriptor(signing):
    e=signing;e.rows[next(iter(e.rows))][8]=b"z"*32
    with pytest.raises(ValueError,match="BTC_ADDRESS_PROVENANCE_MISMATCH"):
        m.guard(e.manifest,e.key,e.plan,e.registry,e.graph)


def test_base_only_plan_depends_on_custody_and_keeps_derivation_highwater(signing):
    e=signing;p=deepcopy(e.plan)
    m.guard(e.manifest,e.key,e.plan,e.registry,e.graph,e.states[0])
    p.update(inputs=p["inputs"][:1],input_keys=p["input_keys"][:1],input_provenance=p["input_provenance"][:1],
        derivation_generation="0",outputs=[dict(amount="9000",script="0014"+"55"*20)])
    e.v.derivation=2;e.v.qualified=False
    m.guard(e.manifest,e.key,p,e.registry,e.graph,e.states[0])
    assert e.states[0].get("btc-input-generation:"+e.manifest.context.hex())["derivation"]=="2"
    e.v.cg=3;p["custody_generation"]="3"
    m.guard(e.manifest,e.key,p,e.registry,e.graph,e.states[0])
    e.v.cg=1;p["custody_generation"]="1"
    with pytest.raises(ValueError,match="BTC_INPUT_RECOVERY_REQUIRED"):
        m.guard(e.manifest,e.key,p,e.registry,e.graph,e.states[0])


def test_wrong_profile_and_old_version_cannot_authorize_new_graph(signing):
    e=signing
    with pytest.raises(ValueError,match="BTC_ADDRESS_SIGNING_PROFILE_REQUIRED"):
        m.guard(e.manifest,e.key,e.plan,e.registry,dict(usage="MINT_ONLY_V1"))
    with pytest.raises(ValueError,match="BTC_ADDRESS_SIGNING_PLAN_REQUIRED"):
        m.guard(e.manifest,e.key,dict(version="2"),e.registry,e.graph)


def commits(e):
    first=[worker.sign_step("btc-commit",e.manifest,k,e.plan,e.registry,s,e.bitcoin) for k,s in zip(e.keys,e.states)]
    return dict(e.plan,commitments=[{str(i+1):c["values"][j] for i,c in enumerate(first)} for j in range(3)])


def test_approved_old_plan_stays_fenced_after_resume_and_cached_nonce_is_retained(signing):
    e=signing;worker.sign_step("btc-commit",e.manifest,e.key,e.plan,e.registry,e.states[0],e.bitcoin)
    sid="btc:"+e.manifest.context.hex()+":"+e.plan["batch_id"]+":0";saved=e.states[0].get(sid)
    for generation,status in ((2,2),(3,0)):
        e.v.cg=generation;e.v.custody=status
        with pytest.raises(ValueError,match="BTC_INPUT_GENERATION_CHANGED"):
            transport.cached_transport_step("btc-commit",e.manifest,e.key,e.plan,e.registry,e.states[0])
    assert e.states[0].get(sid)==saved
    # A different plan at the same batch/input cannot reuse the saved nonce,
    # even when its generation is now current.
    e.plan["custody_generation"]="3"
    with pytest.raises(ValueError,match="NONCE_PLAN_MISMATCH"):
        worker.sign_step("btc-commit",e.manifest,e.key,e.plan,e.registry,e.states[0],e.bitcoin)
    assert e.states[0].get(sid)==saved


def test_nonce_spent_before_crash_cannot_be_reused_or_reexported(signing,monkeypatch):
    e=signing;work=commits(e);save=e.states[0].put
    def crash(key,value):
        if value.get("status")=="share-prepared":raise RuntimeError("SIMULATED_DISK_CRASH")
        save(key,value)
    monkeypatch.setattr(e.states[0],"put",crash)
    with pytest.raises(RuntimeError,match="SIMULATED_DISK_CRASH"):
        worker.sign_step("btc-sign",e.manifest,e.key,work,e.registry,e.states[0],e.bitcoin)
    monkeypatch.setattr(e.states[0],"put",save)
    restarted=SecureState(str(e.states[0].path),"test-state-password")
    for action in ("btc-commit","btc-sign"):
        with pytest.raises(RuntimeError,match="NONCE_ALREADY_SPENT"):
            worker.sign_step(action,e.manifest,e.key,work,e.registry,restarted,e.bitcoin)


def test_missing_common_journal_never_regenerates_address_nonce(signing):
    e=signing;work=commits(e)
    worker.sign_step("btc-sign",e.manifest,e.key,work,e.registry,e.states[0],e.bitcoin)
    Path(str(e.states[0].path)+".spent.sqlite").unlink()
    for action in ("btc-commit","btc-sign"):
        with pytest.raises(RuntimeError,match="NONCE_JOURNAL_MISSING"):
            transport.cached_transport_step(action,e.manifest,e.key,work,e.registry,e.states[0])


def test_generation_change_during_private_signing_burns_nonce_but_exports_no_share(signing,monkeypatch):
    e=signing;work=commits(e);call=crypto.call
    def race(op,**kwargs):
        result=call(op,**kwargs)
        if op=="bitcoin_sign_v1":e.v.dg=3
        return result
    monkeypatch.setattr(crypto,"call",race)
    with pytest.raises(ValueError,match="BTC_INPUT_GENERATION_CHANGED"):
        worker.sign_step("btc-sign",e.manifest,e.key,work,e.registry,e.states[0],e.bitcoin)
    sid="btc:"+e.manifest.context.hex()+":"+e.plan["batch_id"]+":0";saved=e.states[0].get(sid)
    assert saved["status"]=="share-prepared" and "nonces" not in saved
    e.states[0].assert_spent(sid,saved["transcript"])


def test_generation_change_during_public_packet_publication_is_blocked(signing,monkeypatch):
    e=signing;e.v.cg=2
    with pytest.raises(ValueError,match="BTC_INPUT_GENERATION_CHANGED"):
        transport.publish_values(e.manifest,e.key,e.registry,None,e.plan,"commitment",[])


def test_bitcoin_anchor_change_during_preflight_is_retryable_without_nonce(signing,monkeypatch):
    e=signing;call=e.bitcoin.call;n=0
    def changed(method,*args):
        nonlocal n
        if method=="getbestblockhash":
            n+=1;return ("88" if n>1 else "77")*32
        return call(method,*args)
    monkeypatch.setattr(e.bitcoin,"call",changed)
    with pytest.raises(ValueError,match="BTC_INPUT_CORE_CHANGED"):
        worker.sign_step("btc-commit",e.manifest,e.key,e.plan,e.registry,e.states[0],e.bitcoin)
    assert e.states[0].get("btc:"+e.manifest.context.hex()+":"+e.plan["batch_id"]+":0") is None


def test_uint256_generations_remain_exact_and_plan_bytes_bounded(signing):
    e=signing;p=dict(e.plan,custody_generation=str(2**256-1))
    assert decode_plan(serialize_plan(p))["custody_generation"]==str(2**256-1)
    for value in (str(2**256),"01",True,1.0):
        with pytest.raises(ValueError):validate_plan(dict(e.plan,custody_generation=value))
    p=deepcopy(e.plan);p["input_keys"][1]["secret"]="nonce"
    with pytest.raises(ValueError,match="BTC_INPUT_KEY_SCHEMA"):serialize_plan(p)


def test_prepare_automatically_resolves_mixed_inputs_and_retains_canonical_public_plan(signing,monkeypatch):
    e=signing;e.v.bound=False
    board=SimpleNamespace(address=e.plan["signing_board"],functions=Functions(dict(batchRegistry=e.batches.address,
        getPlanHash=lambda batch,raw:keccak(encode(["bytes32","uint256","address","bytes32","bytes32","bytes32"],
            [DOMAIN,31337,e.plan["signing_board"],e.intent.wallet_result,batch,keccak(raw)])))))
    monkeypatch.setattr(payment,"_graph",lambda *_:(e.graph,dict(batches=e.batches,escrow=e.escrow,deposits=e.common,address_registry=e.reg)))
    monkeypatch.setattr(payment,"_bitcoin",lambda *_:e.bitcoin)
    args=SimpleNamespace(batch=e.plan["batch_id"],deadline=200,fee=1000,max_fee=2000,
        inputs=[i["outpoint"] for i in e.plan["inputs"]],withdrawals=e.plan["withdrawal_refs"],signers=[1,2])
    result=payment._prepare(args,e.manifest,e.key,e.registry,e.states[0],board)
    assert result["status"]=="BTC_PLAN_PREPARED" and result["plan"]==e.plan
    assert transport.cached_plan(e.manifest,e.states[0],e.plan["batch_id"])==e.plan


def test_broadcast_route_aggregates_with_the_descriptor_of_each_input(signing,monkeypatch):
    e=signing;work=commits(e)
    shares=[worker.sign_step("btc-sign",e.manifest,k,work,e.registry,s,e.bitcoin) for k,s in zip(e.keys,e.states)]
    share_rows=[{str(i+1):s["values"][j] for i,s in enumerate(shares)} for j in range(3)]
    e.states[0].put("btc-registry:"+e.manifest.context.hex(),dict(graph=e.graph))
    monkeypatch.setattr(transport,"checked_board",lambda *_:SimpleNamespace(address=e.plan["signing_board"]))
    monkeypatch.setattr(transport,"load_plan",lambda *_:e.plan)
    monkeypatch.setattr(transport,"collect",lambda *_:(work["commitments"],share_rows,[],[]))
    monkeypatch.setattr(transport,"bitcoin_for_graph",lambda *_:e.bitcoin)
    e.batch_values["batchState"]=lambda _:2
    observed=[]
    monkeypatch.setattr(worker.BitcoinWorker,"broadcast",lambda self,batch,signed:observed.append(signed) or signed["txid"])
    args=SimpleNamespace(action="btc-broadcast",state=str(e.states[0].path),batch=e.plan["batch_id"],
        signing_board=e.plan["signing_board"],bitcoin_rpc="fixture",bitcoin_cookie="fixture")
    result=transport.dispatch(args,e.manifest,e.key,e.registry,e.states[0])
    assert result["status"]=="BROADCAST" and result["txid"]==m.build(e.plan)["txid"]
    assert len(observed)==1 and observed[0]["wtxid"]!=observed[0]["txid"]


def test_orphaned_generation_anchor_requires_recovery_without_deleting_spent_state(signing):
    e=signing;m.guard(e.manifest,e.key,e.plan,e.registry,e.graph,e.states[0])
    e.states[0].put("prior-spent-receipt",dict(status="retained"));e.v.reorg=True
    # Binding's canonical registration anchor fails too; neither failure may
    # manufacture a new nonce or clear local incident evidence.
    with pytest.raises(ValueError):m.guard(e.manifest,e.key,e.plan,e.registry,e.graph,e.states[0])
    assert e.states[0].get("prior-spent-receipt")==dict(status="retained")


def test_authorized_payment_settlement_recovery_does_not_require_new_generation(signing,monkeypatch):
    e=signing;e.v.cg=9;e.v.derivation=2
    # An already-paid version is recovered before Core/Fresh/generation checks;
    # it creates no new Bitcoin signature and cannot reset a nonce.
    e.escrow.functions.values["payment"]=lambda _:(sha256d(bytes.fromhex(m.build(e.plan)["unsigned"])),0,True,[bytes.fromhex("66"*32)])
    monkeypatch.setattr(payment,"_graph",lambda *_:(e.graph,dict(batches=e.batches,escrow=e.escrow)))
    monkeypatch.setattr(payment,"_validate_binding",lambda *_:None)
    monkeypatch.setattr(payment,"_batch_matches",lambda *_:True)
    monkeypatch.setattr(payment,"_payment_matches",lambda *_:True)
    result=payment._settle(SimpleNamespace(),e.manifest,e.key,e.registry,e.states[0],None,e.plan)
    assert result["status"]=="BTC_PAYMENT_SETTLED" and not e.v.core_calls


def test_explicit_input_recovery_reanchors_only_public_cursor_and_retains_nonce(signing):
    e=signing;worker.sign_step("btc-commit",e.manifest,e.key,e.plan,e.registry,e.states[0],e.bitcoin)
    name="btc-input-generation:"+e.manifest.context.hex();old=e.states[0].get(name)
    # Simulate an orphaned observation anchor without changing the canonical
    # registration anchor or the current allowed generation.
    old["hash"]=(b"o"*32).hex();e.states[0].put(name,old)
    sid="btc:"+e.manifest.context.hex()+":"+e.plan["batch_id"]+":0";saved=e.states[0].get(sid)
    with pytest.raises(ValueError,match="BTC_INPUT_RECOVERY_REQUIRED"):
        m.guard(e.manifest,e.key,e.plan,e.registry,e.graph,e.states[0])
    result=m.recover(e.manifest,e.key,e.plan,e.registry,e.graph,e.states[0])
    assert result["status"]=="BTC_INPUT_GENERATIONS_RECOVERED" and e.states[0].get(sid)==saved
    assert e.states[0].get(name)["hash"]!=(b"o"*32).hex()
    m.guard(e.manifest,e.key,e.plan,e.registry,e.graph,e.states[0])
    receipt_key="btc-input-generation-recovered:"+e.manifest.context.hex()+":"+e.block.hash.hex()
    receipt=e.states[0].get(receipt_key)
    m.recover(e.manifest,e.key,e.plan,e.registry,e.graph,e.states[0])
    assert e.states[0].get(receipt_key)==receipt


def test_input_recovery_cannot_regress_generation_or_clear_financial_pause(signing):
    e=signing;name="btc-input-generation:"+e.manifest.context.hex()
    m.guard(e.manifest,e.key,e.plan,e.registry,e.graph,e.states[0]);old=e.states[0].get(name)
    old["custody"]="3";e.states[0].put(name,old)
    with pytest.raises(ValueError,match="BTC_INPUT_RECOVERY_REQUIRED"):
        m.recover(e.manifest,e.key,e.plan,e.registry,e.graph,e.states[0])
    pause="btc-payments-recovery:"+e.manifest.context.hex();e.states[0].put(pause,dict(batch="other",requires_review=True))
    with pytest.raises(ValueError,match="PAYMENTS_PAUSED_FOR_RECOVERY"):
        m.recover(e.manifest,e.key,e.plan,e.registry,e.graph,e.states[0])
    assert e.states[0].get(name)==old and e.states[0].get(pause)["batch"]=="other"

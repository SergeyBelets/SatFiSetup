"""Real native codec/derivation; explicit Core/EVM/provenance boundary fixtures."""
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3

from signer_agent import address_mint as m
from signer_agent.deposit_intent import DepositIntentV1, PROFILE
from signer_agent.bitcoin_proofs import sha256d

ROOT = Path(__file__).resolve().parents[2]
VECTOR = json.loads((ROOT / "rust/zkspv/derivation-qualification/public-vector.json").read_text())


def address(i): return Web3.to_checksum_address(f"0x{i:040x}")


class Functions:
    def __init__(self, values): self.values = values
    def __getattr__(self, name):
        def invoke(*args):
            def call(block_identifier="latest"):
                value = self.values[name]
                return value(*args) if callable(value) else value
            return SimpleNamespace(call=call)
        return invoke


@pytest.fixture
def funding_env(monkeypatch):
    raw_intent = bytes.fromhex(VECTOR["intent_bytes"]); intent = DepositIntentV1.decode(raw_intent)
    recipient = Web3.to_checksum_address(intent.recipient)
    pool_address = Web3.to_checksum_address(intent.pool); reg_address = Web3.to_checksum_address(intent.deposit_registry)
    view = SimpleNamespace(cg=1, dg=2, custody=0, derivation=0, qualified=True, confirmations=6,
        spent=False, consumed=set(), wallet=set(), reserved=set(), minted=0, allocation=None, minted_intent=None,
        core_calls=[], raw=None, txid=None)
    b = dict(intent=intent, script=bytes.fromhex(VECTOR["script_pubkey"]),
        recipient=recipient, binding=bytes.fromhex(VECTOR["binding_commitment"]), provenance=b"p" * 32)
    reg_values = dict(custodyGeneration=lambda:view.cg, derivationGeneration=lambda:view.dg,
        custodyState=lambda:view.custody, derivationState=lambda:view.derivation,
        bitcoinGenesis=intent.bitcoin_genesis, verifier=address(7), validateMint=lambda *_:keccak(b["script"]))
    def allocation(key):
        if view.allocation is not None:return view.allocation
        return keccak(encode(["address[]", "uint256[]"], [[recipient], [view.minted * 10**10]])) if view.minted else bytes(32)
    common_values = dict(depth=6, spv=address(8), consumed=lambda key:key in view.consumed,
        walletOutput=lambda key:key in view.wallet)
    pool_values = dict(mintedAllocations=allocation, mintedAddressIntents=lambda _:
        view.minted_intent if view.minted_intent is not None else intent.intent_id if view.minted else bytes(32),
        mintedAddressSats=lambda _:view.minted, mpToken=Web3.to_checksum_address(intent.mp_token))
    def node(where, values): return SimpleNamespace(address=where, functions=Functions(values))
    r=node(reg_address,reg_values);common=node(address(11),common_values)
    batch=node(address(12),dict(reservations=lambda key:b"r" * 32 if key in view.reserved else bytes(32)))
    pool=node(pool_address,pool_values)
    verifier=node(address(7),dict(QUALIFIED=lambda:view.qualified))
    spv=node(address(8),dict(getBlockStatus=lambda _:(True,view.confirmations), checkTxInclusion=lambda *_:True))
    w3=SimpleNamespace(eth=SimpleNamespace(chain_id=31337, contract=lambda address,**_: {verifier.address:verifier,spv.address:spv}[address],
        get_block=lambda _:SimpleNamespace(hash=b"h" * 32)))
    pool.w3=w3
    context=dict(registry=r,common=common,batch=batch,pool=pool,number=10)
    monkeypatch.setattr(m,"binding",lambda context,identifier:b if identifier==intent.intent_id else (_ for _ in ()).throw(ValueError("BTC_ADDRESS_BINDING_MISMATCH")))
    def compact(n):return bytes([n]) if n<253 else b"\xfd"+n.to_bytes(2,"little")
    def transaction(inputs=1,outputs=1,target=0,coinbase=False,witness=False):
        vin=b""
        for i in range(inputs):
            vin+=(bytes(32) if coinbase else b"\x11"*32)+(0xffffffff if coinbase else i).to_bytes(4,"little")
            vin+=b"\x02\x00\x00" if coinbase else b"\x00"
            vin+=b"\xff"*4
        vout=b""
        for i in range(outputs):
            s=b["script"] if i==target else bytes.fromhex("6a04534d5031")
            vout+=(1000 if i==target else 0).to_bytes(8,"little")+compact(len(s))+s
        body=compact(inputs)+vin+compact(outputs)+vout
        stripped=b"\x02\x00\x00\x00"+body+b"\x00"*4
        view.raw=(b"\x02\x00\x00\x00\x00\x01"+body+b"\x01\x01\x00"*inputs+b"\x00"*4) if witness else stripped
        view.txid=sha256d(stripped)[::-1].hex()
        return stripped
    transaction()
    class Core:
        policy=None
        def check_network(self):pass
        def call(self,method,*args):
            view.core_calls.append((method,args))
            if method=="getblockhash":return intent.bitcoin_genesis.hex()
            if method=="getrawtransaction":return dict(hex=view.raw.hex(),blockhash="22"*32)
            if method=="getblock":return dict(tx=[view.txid],merkleroot=view.txid,confirmations=view.confirmations)
            if method=="gettxout":return None if view.spent else dict(scriptPubKey=dict(hex=b["script"].hex()),
                value=Decimal("0.00001000"),confirmations=view.confirmations)
            raise AssertionError(method)
    return SimpleNamespace(**locals(),bitcoin=Core())


@pytest.mark.parametrize("inputs,outputs,target,witness",[(1,1,0,False),(257,1,0,False),(1,257,256,False),(2,33,32,True)])
def test_real_codec_strips_witness_and_supports_external_funding_counts(funding_env,inputs,outputs,target,witness):
    e=funding_env; stripped=e.transaction(inputs,outputs,target,witness=witness)
    claim,proof=m.funding(e.context,e.bitcoin,e.view.txid,target,e.intent.intent_id)
    assert proof[0]==stripped and claim[2]==bytes.fromhex(e.view.txid)[::-1]
    assert claim[4]==e.recipient and claim[5:8]==[1000,1,2]
    assert proof[2]==[] and proof[3]==0


@pytest.mark.parametrize("field,value",[("cg",2),("dg",3),("custody",2),("custody",3),("derivation",1),("derivation",2)])
def test_generation_and_status_guard_is_independent_of_admission_active(funding_env,field,value):
    e=funding_env; c=[e.intent.intent_id,e.b["binding"],b"t"*32,0,e.recipient,1000,1,2,e.b["provenance"]]
    setattr(e.view,field,value)
    with pytest.raises(ValueError,match="BTC_MINT_GENERATION_CHANGED"):m.validate_claim(e.context,c)


def test_real_candidate_qualification_gate_is_not_overridden_by_client(funding_env):
    e=funding_env;e.view.qualified=False
    with pytest.raises(ValueError,match="BTC_ADDRESS_VERIFIER_UNQUALIFIED"):
        m.funding(e.context,e.bitcoin,e.view.txid,0,e.intent.intent_id)


@pytest.mark.parametrize("kind",["consumed","wallet","reserved"])
def test_every_input_including_the_257th_is_checked(funding_env,kind):
    e=funding_env;e.transaction(257)
    if kind=="reserved":e.view.reserved.add(keccak(encode(["bytes32","uint32"],[b"\x11"*32,256])))
    else:getattr(e.view,kind).add(keccak(encode(["bytes32","uint32"],[b"\x11"*32,256])))
    with pytest.raises(ValueError,match="BTC_MINT_OUTPOINT_UNAVAILABLE"):
        m.funding(e.context,e.bitcoin,e.view.txid,0,e.intent.intent_id)


def test_ordinary_funding_requires_six_confirmations(funding_env):
    e=funding_env;e.view.confirmations=5
    with pytest.raises(ValueError,match="BTC_MINT_PROOF_OR_ALLOCATION"):
        m.funding(e.context,e.bitcoin,e.view.txid,0,e.intent.intent_id)
    e.view.confirmations=6
    assert m.funding(e.context,e.bitcoin,e.view.txid,0,e.intent.intent_id)[0][5]==1000


def test_coinbase_maturity_stays_100_and_does_not_use_ordinary_depth(funding_env):
    e=funding_env;e.transaction(coinbase=True);e.view.confirmations=99
    with pytest.raises(ValueError,match="BTC_MINT_PROOF_OR_ALLOCATION"):
        m.funding(e.context,e.bitcoin,e.view.txid,0,e.intent.intent_id)
    e.view.confirmations=100
    assert m.funding(e.context,e.bitcoin,e.view.txid,0,e.intent.intent_id)[0][5]==1000


def test_exact_receipt_recovers_after_spend_and_terminal_status_without_core(funding_env):
    e=funding_env;e.view.minted=1000;e.view.spent=True;e.view.custody=3;e.view.derivation=2
    e.view.consumed.add(keccak(encode(["bytes32","uint32"],[bytes.fromhex(e.view.txid)[::-1],0])))
    c=m.receipt_claim(e.context,e.intent.intent_id,bytes.fromhex(e.view.txid)[::-1],0)
    assert m.complete(e.context,c);assert e.view.core_calls==[]
    e.view.minted_intent=b"x"*32
    with pytest.raises(ValueError,match="BTC_ADDRESS_COMPLETION_MISMATCH"):m.complete(e.context,c)


def test_consumed_alone_never_means_success(funding_env):
    e=funding_env;e.view.consumed.add(keccak(encode(["bytes32","uint32"],[bytes.fromhex(e.view.txid)[::-1],0])))
    c=m.receipt_claim(e.context,e.intent.intent_id,bytes.fromhex(e.view.txid)[::-1],0)
    assert not m.complete(e.context,c)


def test_binding_native_descriptor_and_registration_provenance_are_recomputed(funding_env,monkeypatch):
    e=funding_env;monkeypatch.undo();r=e.reg_values;p=e.pool_values
    r.update(graphId=e.intent.custody_graph_id,baseX=bytes.fromhex(VECTOR["base_group"])[1:],
        dkg=Web3.to_checksum_address(e.intent.dkg_registry),walletId=e.intent.wallet_id,walletResult=e.intent.wallet_result,
        keyEpoch=e.intent.key_epoch,servicePolicyHash=e.intent.service_policy_hash,derivationScope=b"s"*32,
        verifierRuntimeHash=b"v"*32,verifierSourceHash=b"o"*32,qualificationCommitment=b"q"*32)
    derived=e.intent.derive(bytes.fromhex(VECTOR["base_group"]))
    d=[bytes.fromhex(derived[k]) for k in ("intent_id","merkle_root","tweak","output_key")]
    d += [derived["output_parity_odd"],bytes.fromhex(derived["binding_commitment"]),bytes.fromhex(derived["wallet_context"])]
    provenance=keccak(encode(["bytes32"]*10+["uint256","bytes32"],[keccak(text="SATOHSI_ADDRESS_VALIDATION_V1"),
        b"s"*32,keccak(text=PROFILE),b"v"*32,b"o"*32,b"q"*32,e.intent.wallet_result,d[6],e.intent.intent_id,d[5],2,b"h"*32]))
    row=[e.raw_intent,e.pool_address,Web3.to_checksum_address(e.intent.mp_token),Web3.to_checksum_address(e.intent.requester),
         e.recipient,keccak(e.b["script"]),e.intent.request_nonce,d,provenance,2,b"h"*32]
    r["getBinding"]=lambda _:row
    e.common_values.update(scriptPools=lambda _:e.pool_address,addressIntents=lambda _:e.intent.intent_id)
    assert m.binding(e.context,e.intent.intent_id)["provenance"]==provenance
    row[8]=b"x"*32
    with pytest.raises(ValueError,match="BTC_ADDRESS_PROVENANCE_MISMATCH"):m.binding(e.context,e.intent.intent_id)

"""Regression: Bitcoin signing must never use an EVM bind visible only at latest."""
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace

import pytest
from web3 import Web3
from web3.datastructures import AttributeDict

from signer_agent import bitcoin_worker, bitcoin_onchain, bitcoin_onchain_payment, crypto
from signer_agent.bitcoin_onchain_plan import onchain_plan_hash, serialize_plan
from signer_agent.bitcoin_proofs import sha256d
from signer_agent.secure_state import SecureState
from test_bitcoin_onchain_security import public_bitcoin_plan
from test_bitcoin_network import policy


def address(number):
    return Web3.to_checksum_address("0x" + format(number, "040x"))


@pytest.fixture
def mainnet_signing_views(public_bitcoin_plan, monkeypatch):
    manifest, plan = public_bitcoin_plan
    plan["network"] = "mainnet"
    manifest.purpose = "BTC_SPEND"
    manifest.registry = address(11)
    manifest.session_id = b"s" * 32
    manifest.context = b"c" * 32
    key = dict(result_hash=plan["wallet_result"], index=1,
               result=dict(group="02" + plan["wallet_script"][4:]))
    built = crypto.call("bitcoin_build", **{name: plan[name] for name in
                        ("inputs", "outputs", "wallet_script", "fee", "max_fee")})
    approved_hash = onchain_plan_hash(manifest, plan)
    root = bytes.fromhex(plan["batch_id"])
    expected = sha256d(bytes.fromhex(built["unsigned"]))
    graph = dict(version="2", bitcoin_network="mainnet", bitcoin_policy=policy(), evm_confirmations="13")
    calls, sends = [], []
    view = SimpleNamespace(bound=True, batch_state=2, replace_anchor=False, replace_after_send=False,
                           funding_scripts=[plan["wallet_script"]], callback_admitted=True)
    confirmed = AttributeDict(dict(number=88, hash=b"confirmed", timestamp=1800000000))
    latest = AttributeDict(dict(number=100, hash=b"latest", timestamp=1800000000))
    reads = 0
    def get_block(number):
        nonlocal reads
        if number == "latest":
            return latest
        assert number == 88
        reads += 1
        if view.replace_anchor and reads > 1:
            return AttributeDict(dict(number=88, hash=b"orphan-replacement", timestamp=1800000000))
        return confirmed
    values = dict(
        dkg=manifest.registry, walletId=manifest.session_id,
        batches=[approved_hash, bytes.fromhex(key["result_hash"]), int(plan["deadline"]), 2],
        reservationRoot=root, replacementOf=bytes(32), reservations=root,
        withdrawalEscrow=address(12),feeReserve=address(0),
        payment=[expected, 0, False, [bytes.fromhex(ref) for ref in plan["withdrawal_refs"]]],
        requests=[address(20), address(21), address(22), 0, 9000 * 10**10, bytes.fromhex(plan["outputs"][0]["script"]), root, 2],
        depositRegistry=address(13), backingSats=9000, walletOutput=False,
    )
    class Functions:
        def __getattr__(self, method):
            def function(*args):
                def call(**kwargs):
                    block = kwargs.get("block_identifier", "latest")
                    calls.append((method, block))
                    result = values[method]
                    if method == "payment" and block == 88 and not view.bound:
                        return [bytes(32), 0, False, []]
                    if method == "batches":
                        return [*result[:3], view.batch_state if block == 88 else 2]
                    return result
                return SimpleNamespace(call=call)
            return function
    contract = SimpleNamespace(functions=Functions())
    registry = SimpleNamespace(contract=object(), w3=SimpleNamespace(eth=SimpleNamespace(
        chain_id=31337, get_block=get_block, contract=lambda **kwargs: contract)))
    def send(*args):
        sends.append(args)
        if view.replace_after_send:
            view.replace_anchor = True
    registry.send = send
    def bitcoin_call(method, *args):
        if method == "getrawtransaction":
            return dict(txid=args[0], vout=[dict(scriptPubKey={"hex": value}) for value in view.funding_scripts])
        assert method == "gettxout"
        return dict(scriptPubKey=dict(hex=plan["wallet_script"]), value=Decimal("0.0001"), confirmations=6, coinbase=False)
    bitcoin = SimpleNamespace(network="mainnet", check_network=lambda: None, call=bitcoin_call)
    monkeypatch.setattr("signer_agent.bitcoin_trust.require_registry", lambda *args: dict(graph=graph))
    monkeypatch.setattr("signer_agent.bitcoin_trust.check_bitcoin_graph", lambda *args: None)
    # This fixture isolates snapshot/nonce ordering. The real source/runtime
    # checks have their own callback_admission and EVM integration tests.
    def callback_check(escrow, request, block):
        calls.append(("validateCallback", block))
        if not view.callback_admitted:
            raise ValueError("BTC_CALLBACK_NOT_ADMITTED")
    monkeypatch.setattr("signer_agent.callback_admission.validate_callback", callback_check)
    state = SimpleNamespace(exclusive=lambda: nullcontext(), get=lambda key: None,
                            assert_unspent=lambda *args: pytest.fail("unconfirmed state reached nonce creation"))
    return SimpleNamespace(manifest=manifest, plan=plan, key=key, graph=graph, registry=registry,
                           bitcoin=bitcoin, calls=calls, sends=sends, view=view, state=state,
                           built=built, contract=contract, values=values, confirmed=confirmed, latest=latest)


def test_all_bitcoin_spend_terms_are_read_from_one_confirmed_evm_block(mainnet_signing_views):
    s = mainnet_signing_views
    built, digest = bitcoin_worker.validate(s.manifest, s.key, s.plan, s.registry, s.bitcoin, s.graph)
    assert built == s.built
    assert digest == onchain_plan_hash(s.manifest, s.plan)
    assert {block for method, block in s.calls if method != "feeReserve"} == {88}
    assert {method for method, _ in s.calls} == {
        "dkg", "walletId", "batches", "reservationRoot", "replacementOf", "reservations",
        "withdrawalEscrow", "payment", "requests", "depositRegistry", "backingSats",
        "validateCallback", "feeReserve",
    }
    assert s.sends == []


def test_ordinary_bitcoin_signing_keeps_packet_snapshot_when_rpc_advances(mainnet_signing_views):
    s = mainnet_signing_views
    confirmed = AttributeDict(dict(number=89, hash=b"advanced", timestamp=s.confirmed.timestamp))
    latest = AttributeDict(dict(number=101, hash=b"latest", timestamp=s.latest.timestamp))
    blocks = {88: s.confirmed, 89: confirmed, "latest": latest}
    s.registry.w3.eth.get_block = blocks.__getitem__
    built, digest = bitcoin_worker.validate(s.manifest, s.key, s.plan, s.registry, s.bitcoin, s.graph,
                                           packet_anchor=s.confirmed)
    assert digest == onchain_plan_hash(s.manifest, s.plan)
    assert built["_packet_anchors"] == (s.confirmed, confirmed)
    assert {block for method, block in s.calls if method != "feeReserve"} == {89}
    assert s.sends == []


@pytest.mark.parametrize("number,hash_,error", [
    (88, b"earlier-branch", "EVM_REORG_RECOVERY_REQUIRED"),
    (89, b"not-yet-confirmed", "EVM_RPC_BEHIND"),
])
def test_ordinary_signing_refuses_changed_or_lagging_packet_anchor_before_private_work(
    mainnet_signing_views, monkeypatch, number, hash_, error,
):
    s = mainnet_signing_views
    monkeypatch.setattr(bitcoin_worker.crypto, "call", lambda *args, **kwargs:
                        pytest.fail("lost packet anchor reached cryptographic signing work"))
    with pytest.raises(ValueError, match=error):
        bitcoin_worker.sign_step("btc-sign", s.manifest, s.key, s.plan, s.registry, s.state, s.bitcoin,
                                 packet_anchor=SimpleNamespace(number=number, hash=hash_))
    assert s.calls == []
    assert s.sends == []


@pytest.mark.parametrize("action", ["btc-commit", "btc-sign"])
def test_unadmitted_callback_cannot_reach_begin_signing_or_private_nonce(mainnet_signing_views, action, monkeypatch):
    s = mainnet_signing_views
    s.view.callback_admitted = False
    s.view.batch_state = 1
    operations = []
    def public_build_only(operation, **kwargs):
        operations.append(operation)
        assert operation == "bitcoin_build", "unadmitted callback reached private cryptography"
        return s.built
    monkeypatch.setattr(crypto, "call", public_build_only)
    with pytest.raises(ValueError, match="BTC_CALLBACK_NOT_ADMITTED"):
        bitcoin_worker.sign_step(action, s.manifest, s.key, s.plan, s.registry, s.state, s.bitcoin)
    assert operations == ["bitcoin_build"]
    assert s.sends == []
    assert ("validateCallback", 88) in s.calls


@pytest.mark.parametrize("action", ["btc-commit", "btc-sign"])
def test_latest_only_payment_bind_cannot_create_nonce_or_signature_share(mainnet_signing_views, action, monkeypatch):
    s = mainnet_signing_views
    s.view.bound = False  # latest reports valid bind; confirmed state has none.
    used_crypto = []
    def public_build_only(op, **params):
        used_crypto.append(op)
        assert op == "bitcoin_build", "unconfirmed bind reached private nonce/sign operation"
        return s.built
    monkeypatch.setattr(crypto, "call", public_build_only)
    with pytest.raises(ValueError, match="ESCROW_PAYMENT_MISMATCH"):
        bitcoin_worker.sign_step(action, s.manifest, s.key, s.plan, s.registry, s.state, s.bitcoin)
    assert used_crypto == ["bitcoin_build"]
    assert {block for method, block in s.calls if method != "feeReserve"} == {88}
    assert s.sends == []


@pytest.mark.parametrize("when", ["before_begin", "after_begin"])
def test_bitcoin_validation_rechecks_anchor_before_private_signing(mainnet_signing_views, when):
    s = mainnet_signing_views
    s.view.batch_state = 1
    s.view.replace_anchor = when == "before_begin"
    s.view.replace_after_send = when == "after_begin"
    with pytest.raises(ValueError, match="BTC_REGISTRY_READ_REORG"):
        bitcoin_worker.validate(s.manifest, s.key, s.plan, s.registry, s.bitcoin, s.graph)
    assert len(s.sends) == (0 if when == "before_begin" else 1)
    assert {block for method, block in s.calls if method != "feeReserve"} == {88}


def test_cached_packet_cannot_treat_latest_only_begin_signing_as_final(mainnet_signing_views, monkeypatch):
    s = mainnet_signing_views
    s.view.batch_state = 1  # latest is Signing, confirmed state is only Reserved.
    monkeypatch.setattr(bitcoin_onchain, "require_registry", lambda *args: dict(graph=s.graph))
    monkeypatch.setattr(bitcoin_onchain, "check_bitcoin_graph", lambda *args: None)
    monkeypatch.setattr(bitcoin_onchain, "contract", lambda *args: s.contract)
    state = SimpleNamespace(exclusive=lambda: nullcontext(),
                            get=lambda name: None if name.startswith("btc-payments-recovery:") else dict(status="nonce-created"))
    assert bitcoin_onchain.cached_transport_step("btc-commit", s.manifest, s.key, s.plan, s.registry, state, s.bitcoin) is None
    assert s.calls == [("batches", 88)]


def test_treasury_payment_never_counts_as_burning_backed_mp(mainnet_signing_views):
    s = mainnet_signing_views
    s.values["requests"][2] = address(0)
    with pytest.raises(ValueError, match="MINER_FEE_CONSUMES_PRINCIPAL"):
        bitcoin_worker.validate(s.manifest, s.key, s.plan, s.registry, s.bitcoin, s.graph)


def test_treasury_payment_may_use_confirmed_unissued_bitcoin(mainnet_signing_views):
    s = mainnet_signing_views
    s.values["requests"][2] = address(0)
    s.values["backingSats"] = 0
    assert bitcoin_worker.validate(s.manifest, s.key, s.plan, s.registry, s.bitcoin, s.graph)[0] == s.built


@pytest.mark.parametrize("action", ["btc-commit", "btc-sign"])
def test_unissued_committed_deposit_cannot_reach_bitcoin_nonce(mainnet_signing_views, action, monkeypatch):
    s = mainnet_signing_views
    s.values["requests"][2] = address(0)
    s.values["backingSats"] = 0
    s.view.batch_state = 1
    s.view.funding_scripts.append("6a38534d5031" + "11" * 52)
    operations = []
    def public_build_only(operation, **kwargs):
        operations.append(operation)
        assert operation == "bitcoin_build", "committed deposit reached private cryptography"
        return s.built
    monkeypatch.setattr(crypto, "call", public_build_only)
    with pytest.raises(ValueError, match="^BTC_UNMINTED_COMMITTED_DEPOSIT$"):
        bitcoin_worker.sign_step(action, s.manifest, s.key, s.plan, s.registry, s.state, s.bitcoin)
    assert operations == ["bitcoin_build"]
    assert s.sends == []


@pytest.fixture
def expired_s_payment(mainnet_signing_views, tmp_path, monkeypatch):
    s = mainnet_signing_views
    s.plan.update(version="5", previous="00" * 32, max_fee=s.plan["fee"])
    s.latest = AttributeDict(dict(number=100, hash=b"latest", timestamp=int(s.plan["deadline"]) + 1))
    s.registry.w3.eth.get_block = lambda number: s.latest if number in ("latest", 100) else s.confirmed
    s.key["participant"] = s.manifest.participants[0]
    s.key["result"].update(signing_share="00" * 31 + "01", key_package="test-key-package")
    s.graph["contracts"] = {
        name: dict(address=contract_address, artifact=artifact_name)
        for name, contract_address, artifact_name in (
            ("batches", s.plan["batch_registry"], "BitcoinBatchRegistry"),
            ("escrow", s.values["withdrawalEscrow"], "BitcoinWithdrawalEscrowV2"),
            ("deposits", s.values["depositRegistry"], "BitcoinDepositRegistry"),
        )
    }
    user_fee_cap = 1000
    root = bytes.fromhex(s.plan["batch_id"])
    digest = onchain_plan_hash(s.manifest, s.plan)
    s.values.update(
        batches=[digest, bytes.fromhex(s.key["result_hash"]), int(s.plan["deadline"]), 2],
        getInputs=[bitcoin_worker.outpoint_hash(s.plan["inputs"][0]["outpoint"])],
        plan=serialize_plan(s.plan, s.manifest), isBound=True,
        minerFee=user_fee_cap, minerFeeLimitSats=user_fee_cap, minimumPayoutSats=9000,
        minP2WPKHSats=294, backingSats=10000,
        getValue=(10000, bytes.fromhex(s.plan["wallet_script"]), True),
    )
    s.values["requests"][4] = 10000 * 10**10
    packets = {}
    s.view.latest_state = 2
    s.view.core_anchor_changes = False
    artifacts = bitcoin_onchain_payment.artifact
    def payment_artifact(kind):
        if kind == "ConfederationPoolV2":
            return dict(abi=[{"type": "function", "name": "minimumPayoutSats"}])
        return artifacts(kind)
    monkeypatch.setattr(bitcoin_onchain_payment, "artifact", payment_artifact)

    class Functions:
        def __getattr__(self, method):
            def call(*args, block_identifier):
                if method == "batchState":
                    return s.view.batch_state if block_identifier == 88 else s.view.latest_state
                if method == "scriptPools":
                    return address(0 if args[0] == Web3.keccak(bytes.fromhex(s.plan["outputs"][0]["script"])) else 20)
                if method == "packet":
                    batch_id, index, kind, sender = args
                    assert batch_id == root
                    return packets.get((index, kind, sender), b"")
                return getattr(s.contract.functions, method)(*args).call(block_identifier=block_identifier)
            return lambda *args: SimpleNamespace(call=lambda **kwargs: call(*args, **kwargs))

    def contract(*, address, abi):
        return SimpleNamespace(address=address, abi=abi, w3=s.registry.w3, functions=Functions())

    s.registry.w3.eth.contract = contract
    s.board = contract(address=s.plan["signing_board"], abi=[])
    original_core_call = s.bitcoin.call
    core_reads = []
    def core_call(method, *args):
        if method == "getbestblockhash":
            core_reads.append(method)
            return "cd" * 32 if s.view.core_anchor_changes and len(core_reads) > 1 else "ab" * 32
        return original_core_call(method, *args)
    s.bitcoin.call = core_call
    s.state = SecureState(tmp_path / "signer.enc", "test-expired-s-state")
    s.packets = packets
    s.signed = []
    native_call = crypto.call
    def local_crypto(op, **kwargs):
        if op == "sign":
            s.signed.append(kwargs)
            return {"share": "00" * 31 + "01"}
        return native_call(op, **kwargs)
    monkeypatch.setattr(crypto, "call", local_crypto)
    s.sid = "btc:" + s.manifest.context.hex() + ":" + s.plan["batch_id"] + ":0"
    s.step = lambda action, **kwargs: bitcoin_worker.sign_step(
        action, s.manifest, s.key, s.plan, s.registry, s.state, s.bitcoin, **kwargs)
    return s


def _publish_commitment(s, participant, commitment):
    address = s.manifest.participants[participant - 1]
    s.packets[0, 0, address] = bitcoin_onchain.packet_payload(
        s.plan, 0, "commitment", str(participant), commitment, s.manifest)


def test_expired_s_payment_can_finish_the_existing_frost_transcript(expired_s_payment):
    s = expired_s_payment
    with pytest.raises(ValueError, match="^BATCH_EXPIRED$"):
        s.step("btc-commit")
    assert s.state.get(s.sid) is None
    commitment = s.step("btc-commit", resume_board=s.board)["values"][0]
    _publish_commitment(s, 1, commitment)
    assert s.step("btc-commit", resume_board=s.board)["values"] == [commitment]
    other = crypto.call("nonce", share="00" * 31 + "02")["commitments"]
    _publish_commitment(s, 2, other)
    with pytest.raises(ValueError, match="^BATCH_EXPIRED$"):
        s.step("btc-sign")
    signed = s.step("btc-sign", resume_board=s.board)
    assert s.step("btc-sign", resume_board=s.board) == signed
    assert len(s.signed) == 1
    assert s.signed[0]["commitments"] == {"1": commitment, "2": other}
    stored = s.state.get(s.sid)
    s.state.assert_spent(s.sid, stored["transcript"])
    assert "nonces" not in stored and s.sends == []


def test_v3_rechecks_mempool_floor_before_entering_signing(expired_s_payment):
    s = expired_s_payment
    s.latest = AttributeDict({**s.latest, "timestamp": int(s.plan["deadline"])})
    s.view.batch_state = 1
    core_call = s.bitcoin.call
    policy = {"loaded": True, "mempoolminfee": Decimal("0.0010"), "minrelaytxfee": Decimal("0.000001"),
              "incrementalrelayfee": Decimal("0.000001")}

    def call(method, *args):
        if method == "getmempoolinfo":
            return policy
        return core_call(method, *args)

    s.bitcoin.call = call
    with pytest.raises(ValueError, match="^BTC_PLAN_BELOW_MEMPOOL_FEE$"):
        s.step("btc-commit")
    assert s.sends == [] and s.state.get(s.sid) is None
    policy["mempoolminfee"] = Decimal("0.000001")
    assert s.step("btc-commit")["status"] == "COMMITTED"
    assert s.sends == [("beginSigning", bytes.fromhex(s.plan["batch_id"]))]


def test_v3_relay_fee_uses_signed_vsize_and_incremental_rbf_fee():
    policy = {"loaded": True, "mempoolminfee": Decimal("0.00001"), "minrelaytxfee": Decimal("0.00001"),
              "incrementalrelayfee": Decimal("0.00001")}
    bitcoin = SimpleNamespace(call=lambda method: policy)
    escrow = SimpleNamespace(functions=SimpleNamespace(minerFee=lambda previous: SimpleNamespace(
        call=lambda **kwargs: 1000)))
    built = {"unsigned": "00" * 111}
    plan = dict(version="5", inputs=[{}], previous="11" * 32, fee="1127")
    # 111 stripped bytes plus 68 witness bytes give 128 virtual bytes.
    with pytest.raises(ValueError, match="^BTC_PLAN_BELOW_MEMPOOL_FEE$"):
        bitcoin_onchain_payment.require_v3_relay_fee(bitcoin, plan, built, escrow, 88)
    plan["fee"] = "1128"
    bitcoin_onchain_payment.require_v3_relay_fee(bitcoin, plan, built, escrow, 88)
    policy["loaded"] = False
    with pytest.raises(ValueError, match="^BTC_MEMPOOL_POLICY_UNAVAILABLE$"):
        bitcoin_onchain_payment.require_v3_relay_fee(bitcoin, plan, built, escrow, 88)


@pytest.mark.parametrize("state,error", [
    ("reserved", "BTC_EXPIRED_SIGNING_NOT_BOUND"),
    ("unexpired", "BTC_EXPIRED_SIGNING_NOT_BOUND"),
    ("not_bound", "BTC_EXPIRED_SIGNING_NOT_BOUND"),
    ("latest_no_signing", "BTC_EXPIRED_SIGNING_NOT_BOUND"),
    ("plan_mismatch", "BTC_PLAN_BINDING"),
    ("fee_mismatch", "ESCROW_PAYMENT_MISMATCH"),
    ("owner_minimum", "BTC_WITHDRAWAL_BELOW_MINIMUM"),
    ("core_tip_changed", "BTC_INPUT_CORE_CHANGED"),
    ("lost_nonce", "NONCE_COMMITMENT_MISMATCH"),
])
def test_expired_s_recovery_keeps_current_contract_and_nonce_guards(expired_s_payment, state, error):
    s = expired_s_payment
    if state == "reserved":
        s.view.batch_state = 1
    elif state == "unexpired":
        s.latest = AttributeDict({**s.latest, "timestamp": int(s.plan["deadline"])})
    elif state == "not_bound":
        s.values["isBound"] = False
    elif state == "latest_no_signing":
        s.view.latest_state = 3
    elif state == "plan_mismatch":
        s.values["plan"] = b""
    elif state == "fee_mismatch":
        s.values["minerFee"] = 999
    elif state == "owner_minimum":
        s.values["minimumPayoutSats"] = 9001
    elif state == "core_tip_changed":
        s.view.core_anchor_changes = True
    else:  # lost_nonce
        _publish_commitment(s, 1, crypto.call("nonce", share="00" * 31 + "01")["commitments"])
    with pytest.raises(ValueError, match=f"^{error}$"):
        s.step("btc-commit", resume_board=s.board)
    assert s.state.get(s.sid) is None and s.signed == [] and s.sends == []


def test_expired_legacy_plan_still_rejects_recovery(mainnet_signing_views):
    s = mainnet_signing_views
    latest = AttributeDict({**s.latest, "timestamp": int(s.plan["deadline"]) + 1})
    old_get_block = s.registry.w3.eth.get_block
    s.registry.w3.eth.get_block = lambda number: latest if number == "latest" else old_get_block(number)
    with pytest.raises(ValueError, match="^BTC_EXPIRED_SIGNING_NOT_BOUND$"):
        bitcoin_worker.validate(s.manifest, s.key, s.plan, s.registry, s.bitcoin, s.graph, resume_board=object())
    assert s.sends == []


@pytest.mark.parametrize("replacement", [False, True], ids=["initial", "rbf"])
def test_v5_payment_terms_pin_owner_minimum_and_miner_cap(expired_s_payment, replacement):
    s = expired_s_payment
    if replacement:
        s.plan["previous"] = "ab" * 32
        s.values["minerFee"] = int(s.plan["fee"]) - 1
    else:
        s.values["requests"][-2:] = [bytes(32), 1]
    contracts = {name: s.registry.w3.eth.contract(address=spec["address"], abi=[])
                 for name, spec in s.graph["contracts"].items()}
    contracts["escrow"].abi = [{"name": "defaultMinerFeeLimitSats"}]
    bitcoin_onchain_payment._terms(contracts, s.plan)
    s.values["minimumPayoutSats"] += 1
    with pytest.raises(ValueError, match="^BTC_WITHDRAWAL_BELOW_MINIMUM$"):
        bitcoin_onchain_payment._terms(contracts, s.plan)
    assert [(method, height) for method, height in s.calls
            if method in ("minerFeeLimitSats", "minimumPayoutSats")] == [
        ("minerFeeLimitSats", 100), ("minimumPayoutSats", 100),
        ("minerFeeLimitSats", 100), ("minimumPayoutSats", 100),
    ]

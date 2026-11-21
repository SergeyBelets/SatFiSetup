"""Regression: Bitcoin signing must never use an EVM bind visible only at latest."""
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace

import pytest
from web3 import Web3
from web3.datastructures import AttributeDict

from signer_agent import bitcoin_worker, bitcoin_onchain, crypto
from signer_agent.bitcoin_onchain_plan import onchain_plan_hash
from signer_agent.bitcoin_proofs import sha256d
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
                           built=built, contract=contract, values=values)


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

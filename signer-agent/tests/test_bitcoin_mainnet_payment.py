"""Mainnet payment decisions must pass the bound policy before irreversible work."""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

from eth_abi import encode
import pytest
from web3 import Web3

from signer_agent import bitcoin_onchain_payment as payment
from test_bitcoin_onchain_security import bitcoin_reserve_intent, public_bitcoin_plan
from test_security_crypto_review import graph
from test_onboarding_mainnet import bitcoin_policy


def mainnet_graph(**extra):
    return dict(version="2", bitcoin_network="mainnet", bitcoin_policy=bitcoin_policy(),
                evm_confirmations="12", **extra)


def call(value):
    return SimpleNamespace(call=lambda **kwargs: value() if callable(value) else value)


@pytest.mark.parametrize("value", ["0.1", 0.1, True, Decimal("NaN"), Decimal("0.000000001"),
                                    Decimal("0.00000001000000000000000000000000001")])
def test_payment_utxo_does_not_accept_rpc_amount_coercion(value):
    wallet = "5120" + "11" * 32
    bitcoin = SimpleNamespace(call=lambda *args: dict(scriptPubKey={"hex": wallet},
                                                     confirmations=1000, coinbase=False, value=value))
    with pytest.raises(ValueError, match="UTXO_AMOUNT_INVALID"):
        payment._unspent(bitcoin, "aa" * 32 + ":0", wallet)


def test_payment_utxo_preserves_one_satoshi_exactly():
    wallet = "5120" + "11" * 32
    bitcoin = SimpleNamespace(call=lambda *args: dict(scriptPubKey={"hex": wallet},
                                    confirmations=1000, coinbase=False, value=Decimal("0.00000001")))
    assert payment._unspent(bitcoin, "aa" * 32 + ":0", wallet)["amount"] == "1"


@pytest.mark.parametrize("graph,plan_network", [(mainnet_graph(), "regtest"), ({}, "mainnet")])
def test_graph_and_plan_cannot_choose_different_bitcoin_networks(graph, plan_network):
    with pytest.raises(ValueError, match="BTC_PLAN_NETWORK_MISMATCH"):
        payment._network_binding(graph, {"network": plan_network})


@pytest.mark.parametrize("action", ["btc-propose", "btc-execute"])
def test_mainnet_stale_guard_prevents_new_authorization_session(action, monkeypatch):
    from signer_agent import bitcoin_onchain

    graph = mainnet_graph()
    plan = {"network": "mainnet", "deadline": "1000"}
    registry = SimpleNamespace(w3=SimpleNamespace(eth=SimpleNamespace(
        get_block=lambda _: SimpleNamespace(timestamp=10))))
    monkeypatch.setattr(payment, "_graph", lambda *args: (graph, {}))
    monkeypatch.setattr(payment, "_validate_binding", lambda *args: None)
    monkeypatch.setattr(payment, "_completed", lambda *args: False)
    monkeypatch.setattr(payment, "_bitcoin", lambda *args: object())
    monkeypatch.setattr(payment, "_terms", lambda *args: pytest.fail("terms after stale guard"))
    monkeypatch.setattr(bitcoin_onchain, "send", lambda *args, **kwargs: pytest.fail("sent EVM transaction"))
    def stale(*args):
        raise ValueError("MAINNET_RELAY_STALE")
    monkeypatch.setattr(payment, "check_bitcoin_graph", stale)
    with pytest.raises(ValueError, match="MAINNET_RELAY_STALE"):
        payment._authorize(action, SimpleNamespace(operation="reserve"), None, None,
                           registry, None, None, plan)


def configure_mainnet_review(item, graph, monkeypatch):
    from signer_agent import bitcoin_trust
    from signer_agent.bitcoin_onchain_plan import onchain_plan_hash
    from signer_agent.bitcoin_worker import outpoint_hash
    from signer_agent.bootstrap import Bootstrap

    original = bitcoin_trust.inspect_registry
    reviewed = original(graph.manifest, graph.key, graph.registry, graph.names["batches"])
    reviewed["graph"].update(mainnet_graph())
    options = dict(bitcoin_network="mainnet", bitcoin_policy=bitcoin_policy(), evm_confirmations="12")
    seen = []
    def inspected(*args, **kwargs):
        assert kwargs == options
        seen.append("enrolled-options")
        return reviewed
    monkeypatch.setattr(bitcoin_trust, "board_options", lambda *args: options)
    monkeypatch.setattr(bitcoin_trust, "inspect_registry", inspected)
    monkeypatch.setattr(bitcoin_trust, "confirmed_block", lambda *args: graph.block)
    plan = item.intent["bitcoin_plan"]
    plan["network"] = "mainnet"
    args = [bytes.fromhex(plan["batch_id"]), onchain_plan_hash(graph.manifest, plan),
            [outpoint_hash(entry["outpoint"]) for entry in plan["inputs"]], int(plan["deadline"])]
    item.intent["hash_args"] = Bootstrap.wire(args)
    item.intent["operation_hash"] = Web3.keccak(encode(["bytes32", "bytes32", "bytes32[]", "uint64"], args)).hex()
    return reviewed["graph"], seen


def test_mainnet_evm_approval_requires_local_core_and_reviewed_network(bitcoin_reserve_intent, graph, monkeypatch):
    item = bitcoin_reserve_intent
    configure_mainnet_review(item, graph, monkeypatch)
    with pytest.raises(ValueError, match="BITCOIN_RPC_REQUIRED"):
        payment.review_intent(item.intent, item.manifest, item.key, item.registry, None,
                              board_address="known-original-board")
    assert item.checked_terms == []


def test_mainnet_evm_approval_rechecks_core_and_utxos_before_returning_verified(bitcoin_reserve_intent, graph, monkeypatch):
    item = bitcoin_reserve_intent
    expected_graph, seen = configure_mainnet_review(item, graph, monkeypatch)
    bitcoin = object()
    def rpc(bound, url, cookie):
        assert bound == expected_graph and url == "local-rpc" and cookie == "local-cookie"
        seen.append("local-core")
        return bitcoin
    def readiness(registry, bound, client):
        assert bound == expected_graph and client is bitcoin
        seen.append("readiness")
    def terms(contracts, plan, client=None, block=None):
        assert block == graph.block.number
        assert client is bitcoin and plan["network"] == "mainnet"
        assert seen[-1] == "readiness"
        seen.append("utxo-review")
    monkeypatch.setattr(payment, "bitcoin_for_graph", rpc)
    monkeypatch.setattr(payment, "check_bitcoin_graph", readiness)
    monkeypatch.setattr(payment, "_terms", terms)
    result = payment.review_intent(item.intent, item.manifest, item.key, item.registry, None,
                                  board_address="known-original-board", bitcoin_rpc="local-rpc",
                                  bitcoin_cookie="local-cookie")
    assert result["status"] == "BTC_PUBLIC_PLAN_VERIFIED"
    assert seen == ["enrolled-options", "local-core", "readiness", "utxo-review"]


@pytest.fixture
def settlement(monkeypatch, tmp_path):
    graph = mainnet_graph()
    settled = [False]
    wire_txid = b"a" * 32
    escrow = SimpleNamespace(address="escrow", functions=SimpleNamespace(
        payment=lambda batch: call(lambda: [wire_txid, 0, settled[0], []]), depth=lambda: call(6)))
    guard = SimpleNamespace(functions=SimpleNamespace(getBlockStatus=lambda block: call([True, 6])))
    relay = SimpleNamespace(address="raw-mainnet-relay")
    contracts = dict(escrow=escrow, spv_guard=guard, relay=relay,
                     batches=SimpleNamespace(functions=SimpleNamespace(batchState=lambda batch: call(2))))
    plan = dict(network="mainnet", batch_id="aa" * 32)
    proof = dict(confirmations="6", block_hash="bb" * 32, raw="00", siblings=[], index="0")
    bitcoin = object()
    monkeypatch.setattr(payment, "_graph", lambda *args: (graph, contracts))
    monkeypatch.setattr(payment, "_validate_binding", lambda *args: None)
    monkeypatch.setattr(payment, "_batch_matches", lambda *args: True)
    monkeypatch.setattr(payment, "_payment_matches", lambda *args: True)
    monkeypatch.setattr(payment, "_txid", lambda *args: wire_txid)
    monkeypatch.setattr(payment, "_bitcoin", lambda *args: bitcoin)
    monkeypatch.setattr(payment, "inclusion", lambda *args: deepcopy(proof))
    monkeypatch.setattr(payment, "check_bitcoin_graph", lambda *args: None)
    from signer_agent import bitcoin_trust
    from signer_agent.relay_batch import DurableRelayBatch
    block = SimpleNamespace(number=100, hash=b"a" * 32)
    registry = SimpleNamespace(identity_file=str(tmp_path / "identity"),
        w3=SimpleNamespace(eth=SimpleNamespace(chain_id=1, get_block=lambda _: block)))
    monkeypatch.setattr(bitcoin_trust, "confirmed_block", lambda *args: block)
    def canonical_sender(identity, w3, address, policy, confirmations):
        assert identity == registry.identity_file and w3 is registry.w3
        assert address == relay.address and policy == graph["bitcoin_policy"] and confirmations == 12
        return lambda raws: dict(status="PENDING")
    monkeypatch.setattr(DurableRelayBatch, "for_rpc", canonical_sender)
    return SimpleNamespace(graph=graph, contracts=contracts, plan=plan, settled=settled,
                           proof=proof, bitcoin=bitcoin, relay=relay,
                           run=lambda: payment._settle(None, None, None, registry, None, None, plan))


def test_mainnet_settlement_waits_for_bounded_relay_and_evm_finality(settlement, monkeypatch):
    from signer_agent import bitcoin_network, bitcoin_onchain
    item = settlement
    sent = []
    def send(*args, **kwargs):
        sent.append((args, kwargs))
        raise ValueError("BTC_TRANSACTION_UNCONFIRMED")
    def sync(bitcoin, relay, policy, sender, **kwargs):
        assert bitcoin is item.bitcoin and relay is item.relay
        assert policy == item.graph["bitcoin_policy"] and kwargs["evm_confirmations"] == 12
        assert kwargs["send_headers"](["ab" * 80]) == {"status": "PENDING"}
        return dict(status="IN_PROGRESS", submitted="1", reason="EVM_HEADER_PENDING")
    monkeypatch.setattr(bitcoin_onchain, "send", send)
    monkeypatch.setattr(bitcoin_network, "sync_relay", sync)
    result = item.run()
    assert result["status"] == "AWAITING_BITCOIN_RELAY"
    assert sent == [] and item.settled == [False]  # Header sends use the canonical relay journal.


def test_mainnet_settlement_uses_guard_not_raw_relay_status(settlement, monkeypatch):
    from signer_agent import bitcoin_network, bitcoin_onchain
    item = settlement
    monkeypatch.setattr(bitcoin_network, "sync_relay", lambda *args, **kwargs: dict(status="MAINNET_RELAY_READY"))
    item.contracts["spv_guard"].functions.getBlockStatus = lambda block: call([False, 0])
    monkeypatch.setattr(bitcoin_onchain, "send", lambda *args, **kwargs: pytest.fail("stale proof was submitted"))
    with pytest.raises(ValueError, match="PAYMENT_DEPTH"):
        item.run()
    assert item.settled == [False]


def test_mainnet_settlement_rechecks_bitcoin_proof_then_submits_exact_escrow_call(settlement, monkeypatch):
    from signer_agent import bitcoin_network, bitcoin_onchain
    item = settlement
    monkeypatch.setattr(bitcoin_network, "sync_relay", lambda *args, **kwargs: dict(status="MAINNET_RELAY_READY"))
    observations = []
    def inclusion(*args):
        observations.append(True)
        return deepcopy(item.proof)
    def send(registry, address, artifact, method, args, *, graph):
        assert observations == [True, True]
        assert (address, artifact, method) == ("escrow", "BitcoinWithdrawalEscrow", "settle")
        assert graph == item.graph
        assert args == [bytes.fromhex("aa" * 32), b"\x00", bytes.fromhex("bb" * 32), [], 0]
        item.settled[0] = True
    monkeypatch.setattr(payment, "inclusion", inclusion)
    monkeypatch.setattr(bitcoin_onchain, "send", send)
    assert item.run()["status"] == "BTC_PAYMENT_SETTLED"
    assert item.settled == [True]


def test_mainnet_settlement_refuses_changed_network_before_bitcoin_rpc(settlement, monkeypatch):
    settlement.plan["network"] = "regtest"
    monkeypatch.setattr(payment, "_bitcoin", lambda *args: pytest.fail("unbound Bitcoin RPC"))
    with pytest.raises(ValueError, match="BTC_PLAN_NETWORK_MISMATCH"):
        settlement.run()

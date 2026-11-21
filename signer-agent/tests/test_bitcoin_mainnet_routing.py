"""Mainnet trust/transport boundaries with explicit synthetic RPC fixtures."""
from copy import deepcopy
from types import SimpleNamespace
from web3.datastructures import AttributeDict
import pytest
from web3 import Web3

from signer_agent import bitcoin_trust as trust, bitcoin_onchain as transport, bitcoin_worker as worker
from signer_agent.bootstrap import artifact
from signer_agent.bitcoin_network import header_hash, relay_constructor_args
from signer_agent.bitcoin_onchain_plan import onchain_plan_hash
from test_bitcoin_network import policy
from test_security_crypto_review import graph
from test_bitcoin_onchain_security import public_bitcoin_plan, cached_bitcoin_transport


def mainnet_graph():
    return dict(version="2", bitcoin_network="mainnet", bitcoin_policy=policy(), evm_confirmations="3")


@pytest.fixture
def mainnet_contracts(graph):
    graph.names["spv_guard"] = Web3.to_checksum_address("0x" + "99" * 20)
    graph.methods["spv_guard"] = dict(relay=graph.names["relay"], maxTipAgeSeconds=86400,
                                     testHelpersEnabled=False)
    graph.methods["escrow"]["spv"] = graph.names["spv_guard"]
    graph.methods["deposits"]["spv"] = graph.names["spv_guard"]
    graph.methods["relay"].update(checkpointHash=header_hash(bytes.fromhex(policy()["checkpoint_header"])),
                                 checkpointHeight=32256,
                                 checkpointChainWork=int(policy()["checkpoint_chainwork"], 16), maxReorgDepth=6)
    for name, kind in (("relay", "MainnetHeaderRelay"), ("spv_guard", "MainnetSPVGuard")):
        graph.codes[graph.names[name]] = bytes.fromhex(artifact(kind)["deployedBytecode"]["object"].removeprefix("0x"))
    return graph


def test_mainnet_graph_binds_guard_checkpoint_constructor_and_confirmation_policy(mainnet_contracts, monkeypatch):
    g = mainnet_contracts
    origins = {}
    from signer_agent import contract_provenance
    original = contract_provenance.prove_creation
    def prove(w3, address, kind, args, block):
        origins[kind] = args
        return original(w3, address, kind, args, block)
    monkeypatch.setattr(contract_provenance, "prove_creation", prove)
    result = trust.inspect_registry(g.manifest, g.key, g.registry, g.names["batches"],
                                    **trust.graph_options(mainnet_graph()))
    public = result["graph"]
    assert public["version"] == "2" and public["bitcoin_network"] == "mainnet"
    assert public["evm_confirmations"] == "3" and public["bitcoin_policy"] == policy()
    assert origins["MainnetHeaderRelay"] == relay_constructor_args(policy())
    assert origins["MainnetSPVGuard"] == [g.names["relay"], 86400]
    assert origins["BitcoinWithdrawalEscrow"][1] == g.names["spv_guard"]
    assert origins["BitcoinDepositRegistry"][0] == g.names["spv_guard"]
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        trust.inspect_registry(g.manifest, g.key, g.registry, g.names["batches"])


@pytest.mark.parametrize("component,field,value", [
    ("spv_guard", "maxTipAgeSeconds", 86401), ("relay", "maxReorgDepth", 7),
    ("relay", "checkpointChainWork", 1), ("relay", "checkpointHash", b"x" * 32),
])
def test_mainnet_graph_rejects_self_reported_policy_drift(mainnet_contracts, component, field, value):
    g = mainnet_contracts
    g.methods[component][field] = value
    with pytest.raises(ValueError, match="MAINNET_RELAY_POLICY_MISMATCH"):
        trust.inspect_registry(g.manifest, g.key, g.registry, g.names["batches"],
                               **trust.graph_options(mainnet_graph()))


def test_bitcoin_network_changes_public_authorization_hash(public_bitcoin_plan):
    manifest, plan = public_bitcoin_plan
    alternate = dict(plan, network="mainnet")
    assert onchain_plan_hash(manifest, plan) != onchain_plan_hash(manifest, alternate)


@pytest.mark.parametrize("action", ["btc-commit", "btc-sign"])
def test_cached_mainnet_packets_require_fresh_relay_before_publication(cached_bitcoin_transport, monkeypatch, action):
    t = cached_bitcoin_transport
    t.plan["network"] = "mainnet"
    monkeypatch.setattr(transport, "require_registry", lambda *args: {"graph": mainnet_graph()})
    def refuse(*args):
        raise ValueError("MAINNET_RELAY_STALE")
    monkeypatch.setattr(transport, "check_bitcoin_graph", refuse)
    with pytest.raises(ValueError, match="MAINNET_RELAY_STALE"):
        t.cached(action)
    assert t.calls == [] and t.publications == []


def test_network_swap_cannot_reach_private_nonce_or_share(cached_bitcoin_transport, monkeypatch):
    t = cached_bitcoin_transport
    monkeypatch.setattr(trust, "require_registry", lambda *args: {"graph": mainnet_graph()})
    monkeypatch.setattr(worker, "validate", lambda *args: pytest.fail("network swap reached signing"))
    with pytest.raises(ValueError, match="BITCOIN_NETWORK_MISMATCH"):
        worker.sign_step("btc-sign", t.manifest, t.key, t.plan, t.registry, t.state, None)
    assert t.calls == []


def test_mainnet_live_check_uses_confirmed_evm_snapshot_and_guard(monkeypatch):
    graph = dict(mainnet_graph(), contracts={"relay": {"address": "relay"}, "spv_guard": {"address": "guard"}})
    blocks = {n: AttributeDict(dict(number=n, hash=bytes([n]) * 32)) for n in (98, 100)}
    calls = []
    guard = SimpleNamespace(functions=SimpleNamespace(isFresh=lambda: SimpleNamespace(
        call=lambda **kw: calls.append(kw["block_identifier"]) or True)))
    relay = object()
    eth = SimpleNamespace(chain_id=31337, get_block=lambda n: blocks[100 if n == "latest" else n],
                          contract=lambda address, abi: relay if address == "relay" else guard)
    registry = SimpleNamespace(w3=SimpleNamespace(eth=eth))
    from signer_agent import bitcoin_network
    monkeypatch.setattr(bitcoin_network, "inspect_relay", lambda *args, **kw: calls.append(kw["block_identifier"]) or {})
    bitcoin = SimpleNamespace(network="mainnet", policy=policy())
    assert trust.check_bitcoin_graph(registry, graph, bitcoin) == {}
    assert calls == [98, 98]
    bitcoin.network = "regtest"
    with pytest.raises(ValueError, match="BITCOIN_NETWORK_MISMATCH"):
        trust.check_bitcoin_graph(registry, graph, bitcoin)


def test_mainnet_transaction_profile_never_falls_back_to_integration():
    graph = mainnet_graph()
    assert trust.transaction_profile(graph) == dict(mode="backed", bitcoin_network="mainnet", evm_confirmations="3")
    invite = dict(version="4", bitcoin_network="mainnet", bitcoin_policy=policy(), evm_confirmations="3")
    assert trust.invitation_options(invite) == trust.graph_options(graph)
    assert trust.transaction_profile({}) == dict(mode="integration", bitcoin_network="regtest")

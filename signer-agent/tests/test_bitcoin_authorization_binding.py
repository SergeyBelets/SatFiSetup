"""Equivalent legacy defaults must not block an explicitly enrolled V6 payment."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from signer_agent import bitcoin_trust
from signer_agent.bitcoin_onchain_payment import _auth_board
from test_bitcoin_network import policy
from test_independent_networks import public_testnet_policy
from test_security_crypto_review import graph


@pytest.fixture
def binding(graph, monkeypatch):
    inspected = bitcoin_trust.inspect_registry(
        graph.manifest, graph.key, graph.registry, graph.names["batches"]
    )["graph"]
    invite = dict(
        version="6", bitcoin_network="regtest", evm_confirmations="1",
        participants=list(graph.manifest.participants),
        sessions=dict(btc_ceremony=graph.manifest.session_id.hex(),
                      federation_ceremony=inspected["evm_dkg_id"]),
    )
    transport = SimpleNamespace(functions=SimpleNamespace(
        registry=lambda: SimpleNamespace(call=lambda: graph.manifest.registry)))
    monkeypatch.setattr("signer_agent.onchain_transport.invitation",
                        lambda *args: (transport, invite))

    def authorize():
        return _auth_board(SimpleNamespace(board="enrolled-board"), graph.manifest,
                           graph.registry, inspected)

    return SimpleNamespace(fixture=graph, graph=inspected, invite=invite,
                           transport=transport, authorize=authorize)


@pytest.mark.parametrize("depth", ["1", "3"])
def test_v6_payment_accepts_inspected_graph_at_enrolled_confirmation_depth(binding, depth):
    b = binding
    inspected = bitcoin_trust.inspect_registry(
        b.fixture.manifest, b.fixture.key, b.fixture.registry,
        b.fixture.names["batches"], evm_confirmations=depth,
    )["graph"]
    b.graph.clear()
    b.graph.update(inspected)
    b.invite["evm_confirmations"] = depth
    assert b.graph["version"] == ("1" if depth == "1" else "2")
    before = deepcopy((b.graph, b.invite))
    assert b.authorize() == (b.transport, "federation_ceremony")
    assert (b.graph, b.invite) == before


def test_legacy_payment_keeps_implicit_regtest_defaults(binding):
    binding.invite["version"] = "5"
    assert binding.authorize() == (binding.transport, "federation_ceremony")


@pytest.mark.parametrize("invite_depth,graph_depth", [("3", "1"), ("1", "3")])
def test_payment_rejects_confirmation_policy_mismatch(binding, invite_depth, graph_depth):
    binding.invite["evm_confirmations"] = invite_depth
    if graph_depth != "1":
        binding.graph.update(version="2", bitcoin_network="regtest",
                             evm_confirmations=graph_depth)
    with pytest.raises(ValueError, match="^BTC_BOARD_WALLET_MISMATCH$"):
        binding.authorize()


@pytest.mark.parametrize("network", ["mainnet", "testnet4"])
def test_payment_preserves_full_public_bitcoin_policy_binding(binding, network):
    selected = policy() if network == "mainnet" else public_testnet_policy()
    binding.invite.update(bitcoin_network=network, bitcoin_policy=selected)
    binding.graph.update(version="2", bitcoin_network=network,
                         bitcoin_policy=deepcopy(selected), evm_confirmations="1")
    assert binding.authorize() == (binding.transport, "federation_ceremony")
    binding.graph["bitcoin_policy"]["max_relay_lag_blocks"] = "1"
    with pytest.raises(ValueError, match="^BTC_BOARD_WALLET_MISMATCH$"):
        binding.authorize()


@pytest.mark.parametrize("public_invite", [False, True])
def test_payment_rejects_public_bitcoin_and_regtest_mismatch(binding, public_invite):
    public = dict(bitcoin_network="mainnet", bitcoin_policy=policy(), evm_confirmations="1")
    if public_invite:
        binding.invite.update(public)
    else:
        binding.graph.update(version="2", **public)
    with pytest.raises(ValueError, match="^BTC_BOARD_WALLET_MISMATCH$"):
        binding.authorize()


@pytest.mark.parametrize("field", ["registry", "wallet", "participants", "missing_role", "duplicate_role"])
def test_payment_keeps_wallet_and_authorization_origin_binding(binding, field):
    if field == "registry":
        binding.transport.functions.registry = lambda: SimpleNamespace(call=lambda: "other-registry")
    elif field == "wallet":
        binding.invite["sessions"]["btc_ceremony"] = "00" * 32
    elif field == "participants":
        binding.invite["participants"].reverse()
    elif field == "missing_role":
        del binding.invite["sessions"]["federation_ceremony"]
    else:
        binding.invite["sessions"]["custodian_ceremony"] = binding.graph["evm_dkg_id"]
    with pytest.raises(ValueError, match="^BTC_BOARD_WALLET_MISMATCH$"):
        binding.authorize()

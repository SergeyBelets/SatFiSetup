"""Custody admission derives a Bitcoin network from reviewed payment SPV code."""

from types import SimpleNamespace

import pytest
from web3 import Web3

from signer_agent import custody_network as network


BLOCK = 50
GUARD = "0x" + "11" * 20
RELAY = "0x" + "22" * 20
STATE = "0x" + "33" * 20
SOURCE = "0x" + "44" * 20
TOKEN = "0x" + "55" * 20


def contract(address, bindings):
    def method(name):
        def called(*args):
            def read(*, block_identifier):
                assert block_identifier == BLOCK
                return bindings[name]
            return SimpleNamespace(call=read)
        return called

    class Functions:
        def __getattr__(self, name):
            return method(name)

    return SimpleNamespace(address=address, functions=Functions())


@pytest.fixture
def rpc(monkeypatch):
    deployments = {}
    origins = []

    def get_code(address, *, block_identifier):
        assert block_identifier == BLOCK
        name, _, overrides = deployments[address]
        if "code" in overrides:
            return overrides["code"]
        return bytes.fromhex(network.artifact(name)["deployedBytecode"]["object"].removeprefix("0x"))

    def prove(w3, address, kind, args, block, *, previous_build=False):
        assert block == BLOCK and args is None and kind == deployments[address][0]
        assert previous_build is (kind == "MiningPoolV1")
        origins.append(kind)
        return {"method": deployments[address][2].get("method", "direct")}

    monkeypatch.setattr(network, "prove_creation", prove)
    w3 = SimpleNamespace(eth=SimpleNamespace(get_code=get_code, contract=lambda address, abi: contract(address, deployments[address][1])))
    return w3, deployments, origins


@pytest.mark.parametrize("kind,expected", [("MainnetHeaderRelay", "mainnet"), ("Testnet4HeaderRelay", "testnet4")])
def test_network_comes_from_authenticated_guard_child(rpc, kind, expected):
    w3, deployments, origins = rpc
    deployments[GUARD] = ("MainnetSPVGuard", {"relay": RELAY, "bitcoinGenesis": bytes(32)}, {})
    deployments[RELAY] = (kind, {}, {})
    assert network.spv_network(w3, GUARD, BLOCK) == expected
    assert origins == ["MainnetSPVGuard", kind]


@pytest.mark.parametrize("expected,adapter_kind,state_kind,image", [
    ("mainnet", "ZkSpvAdapter", "ZkBitcoinStateV2", network.MAINNET_IMAGE),
    ("testnet4", "ZkTestnet4SpvAdapter", "ZkBitcoinTestnet4StateV2", network.TESTNET4_IMAGE),
])
def test_zk_requires_canonical_children_and_pinned_image(rpc, expected, adapter_kind, state_kind, image):
    w3, deployments, origins = rpc
    verifier, adapter = network._child(STATE, 1), network._child(STATE, 2)
    deployments[verifier] = ("RiscZeroGroth16Verifier", {"VERSION": "3.1.0", "SELECTOR": network.VERIFIER_SELECTOR}, {})
    deployments[adapter] = (adapter_kind, {"state": STATE}, {})
    bindings = {"adapter": adapter, "verifier": verifier, "programImageId": bytes.fromhex(image),
                "verifierCodeHash": Web3.keccak(w3.eth.get_code(verifier, block_identifier=BLOCK))}
    deployments[STATE] = (state_kind, bindings, {})
    assert network.spv_network(w3, adapter, BLOCK) == expected
    assert origins == [state_kind]
    bindings["programImageId"] = bytes(32)
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_SPV_UNSUPPORTED"):
        network.spv_network(w3, adapter, BLOCK)


def test_unknown_payment_code_and_unproved_origin_fail_closed(rpc):
    w3, deployments, _ = rpc
    deployments[GUARD] = ("MainnetSPVGuard", {"relay": RELAY}, {"code": b"unknown SPV"})
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_SPV_UNSUPPORTED"):
        network.spv_network(w3, GUARD, BLOCK)
    deployments[GUARD] = ("MainnetSPVGuard", {"relay": RELAY}, {"method": "unverified"})
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_SPV_UNSUPPORTED"):
        network.spv_network(w3, GUARD, BLOCK)


def test_review_checks_enrolled_source_network_without_changing_descriptor(monkeypatch):
    expected = dict(escrow=SOURCE, token=TOKEN, escrow_hash="0x" + "61" * 32)
    monkeypatch.setattr(network, "_node", lambda w3, address, kind, block: contract(address, {"poolDAO": SOURCE}))
    seen = []

    def pinned_source(w3, mining, block, *, enrolled):
        assert mining == SOURCE and block == BLOCK
        assert enrolled
        seen.append(mining)
        return expected.copy(), "mainnet"

    monkeypatch.setattr(network, "source_graph", pinned_source)
    gateway = contract(RELAY, {"bitcoinGenesis": bytes.fromhex(network.genesis("mainnet")[2:])})
    invitation = dict(bitcoin_network="mainnet", bitcoin_custody={"sources": [expected]})
    assert network.review_gateway_network(None, gateway, invitation, BLOCK) is None and seen == [SOURCE]
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_NETWORK_MISMATCH"):
        network.review_source(None, expected, "testnet4", BLOCK)
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_SOURCE_GRAPH"):
        network.review_source(None, dict(expected, escrow_hash="0x" + "33" * 32), "mainnet", BLOCK)


def test_enrolled_source_requires_exact_previous_mining_hash_and_constructor(rpc):
    from signer_agent.bitcoin_trust import previous_artifact

    w3, deployments, origins = rpc
    escrow, deposits, batches = ("0x" + digit * 40 for digit in ("6", "7", "8"))
    payout = bytes.fromhex("5120" + "19" * 32)
    compiled = previous_artifact("MiningPoolV1")
    code = bytes.fromhex(compiled["deployedBytecode"]["object"].removeprefix("0x"))
    deployments[SOURCE] = ("MiningPoolV1", dict(mpToken=TOKEN, escrow=escrow, deposits=deposits,
        payoutScript=payout), {"code": code})
    deployments[TOKEN] = ("PoolMpToken", {"poolDAO": SOURCE}, {})
    deployments[escrow] = ("BitcoinWithdrawalEscrow", dict(depositRegistry=deposits, batches=batches, spv=RELAY), {})
    deployments[deposits] = ("BitcoinDepositRegistry", dict(spendRecorder=escrow, spv=RELAY,
        poolScripts=Web3.keccak(payout)), {})
    deployments[batches] = ("BitcoinBatchRegistry", {"withdrawalEscrow": escrow}, {})
    deployments[RELAY] = ("RegtestHeaderRelay", {"testHelpersEnabled": False}, {})

    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        network.source_graph(w3, SOURCE, BLOCK)
    descriptor, actual_network = network.source_graph(w3, SOURCE, BLOCK, enrolled=True)
    assert actual_network == "regtest"
    assert descriptor["mining_hash"] == Web3.to_hex(Web3.keccak(code))
    assert "MiningPoolV1" in origins
    network.review_source(w3, descriptor, "regtest", BLOCK)
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_SOURCE_GRAPH"):
        network.review_source(w3, dict(descriptor, mining_hash="0x" + "aa" * 32), "regtest", BLOCK)


def test_legacy_does_not_admit_unexecuted_or_different_binding(rpc, monkeypatch):
    w3, deployments, _ = rpc
    operation = bytes.fromhex("16" * 32)
    gateway = SimpleNamespace(address=GUARD)
    issuer_data = dict(authorization=STATE, custodyTransfer=GUARD, getSetCustodyTransferOperationHash=operation)
    issuer = contract(SOURCE, issuer_data)
    gateway_kind = "BitcoinCustodyTransferV2"
    deployments[GUARD] = (gateway_kind, {}, {"code": b"reviewed historical fixture"})
    monkeypatch.setattr(network, "known_legacy_gateway_runtime", lambda code, name: bool(code) and name == gateway_kind)
    approval = {"executedOperations": False, "operationTargets": SOURCE}
    monkeypatch.setattr(network, "_node", lambda *a: contract(STATE, approval))
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_GATEWAY_BINDING"):
        network.require_enrolled_legacy(w3, gateway, gateway_kind, issuer, operation, BLOCK)
    approval["executedOperations"] = True
    assert network.require_enrolled_legacy(w3, gateway, gateway_kind, issuer, operation, BLOCK) is None
    issuer_data["custodyTransfer"] = RELAY
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_GATEWAY_BINDING"):
        network.require_enrolled_legacy(w3, gateway, gateway_kind, issuer, operation, BLOCK)

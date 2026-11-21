"""Real deployed relay origin checks through an RPC that refuses historical state.

Only provenance is exercised here. Bitcoin observations and header submission
are stubbed; existing relay/payment E2E cover those independent paths.
"""
import json
from types import SimpleNamespace

from eth_account import Account
from web3 import Web3

from signer_agent import relay_keeper
from signer_agent.bitcoin_network import relay_constructor_args
from signer_agent.contract_provenance import prove_creation
from test_dkg_processes import RPC, deploy
from test_mainnet_bootstrap import HistoricalFixtureCore


def test_relay_cold_locator_and_restart_work_without_historical_state(tmp_path, monkeypatch, record_property):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337
    policy = HistoricalFixtureCore().public_policy()
    relay = deploy(w3, "MainnetHeaderRelay", *relay_constructor_args(policy))
    creation = w3.eth.block_number
    w3.provider.make_request("anvil_mine", [hex(64)])
    latest = w3.eth.block_number
    code_hash = Web3.keccak(w3.eth.get_code(relay.address)).hex()
    calls = []
    original_provider = Web3.HTTPProvider

    class NoArchiveProvider(original_provider):
        def make_request(self, method, params):
            calls.append((str(method), params))
            if method in ("eth_getCode", "eth_getStorageAt", "eth_call"):
                block = params[-1]
                if isinstance(block, str) and block.startswith("0x") and int(block, 16) < latest:
                    raise AssertionError("Historical state requested")
            return super().make_request(method, params)

    # The DAO loader already supplies the saved creation block. It may replay
    # the public receipt/initcode without trusting the saved JSON as authority.
    direct = Web3(NoArchiveProvider(RPC))
    proof = prove_creation(direct, relay.address, "MainnetHeaderRelay",
                           relay_constructor_args(policy), latest, birth_hint=creation)
    assert int(proof["block_number"]) == creation

    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"private_key": Account.create().key.hex()}))
    identity.chmod(0o600)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(policy))
    args = SimpleNamespace(rpc=RPC, chain_id=31337, evm_confirmations=1,
        bitcoin_policy=policy_file, relay=relay.address, relay_code_hash=code_hash,
        relay_creation_block=creation, identity_file=identity,
        bitcoin_rpc="http://unused.invalid", bitcoin_cookie="unused")
    monkeypatch.setattr(relay_keeper.Web3, "HTTPProvider", NoArchiveProvider)
    monkeypatch.setattr(relay_keeper, "BitcoinRPC", lambda *a, **k: object())
    monkeypatch.setattr(relay_keeper.DurableRelayBatch, "for_rpc", lambda *a, **k: object())
    monkeypatch.setattr(relay_keeper, "sync_relay", lambda *a, **k: {"status": "MAINNET_RELAY_READY"})
    calls.clear()
    assert relay_keeper.RelayKeeper(args).tick()["status"] == "MAINNET_RELAY_READY"
    cold = list(calls)
    assert any(method == "eth_getTransactionReceipt" for method, _ in cold)
    assert any(method == "eth_getBlockByNumber" and params[1] for method, params in cold)
    calls.clear()
    args.relay_creation_block = None
    assert relay_keeper.RelayKeeper(args).tick()["status"] == "MAINNET_RELAY_READY"
    warm = list(calls)
    assert not any(method == "eth_getTransactionReceipt" for method, _ in warm)
    assert not any(method == "eth_getBlockByNumber" and params[1] for method, params in warm)
    assert any(method == "eth_getCode" for method, _ in warm)
    record_property("provenance_history", json.dumps(dict(
        scope="Real Anvil relay deployment; historical state RPC is rejected; Bitcoin sync stubbed",
        historical_state_requests=0, cold_rpc_calls=len(cold), restart_rpc_calls=len(warm),
        restart_receipt_reads=0, restart_full_block_reads=0,
        current_runtime_and_canonical_anchors_checked=True)))

"""Canonical relay transactions survive changed callers, tips and competing relayers."""
import json
import shutil
import socket
import subprocess
import time
from types import SimpleNamespace

from eth_account import Account
import pytest
from web3 import Web3
from web3.eth import Eth

from signer_agent.bitcoin_network import relay_artifact, relay_constructor_args, sync_relay
from signer_agent.bootstrap import artifact
from signer_agent.operations import Journal
from signer_agent.relay_batch import DurableRelayBatch
from test_dkg_processes import deploy
from test_network_matrix import HistoricalPublicCore


@pytest.mark.parametrize("network", ["mainnet", "testnet4"])
def test_relay_shared_sender_tip_growth_crash_and_competing_operator(tmp_path, monkeypatch, network):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    rpc = f"http://127.0.0.1:{port}"
    w3 = Web3(Web3.HTTPProvider(rpc))
    core = HistoricalPublicCore(network)
    policy, first = dict(core.public_policy(), max_reorg_depth="6"), core.checkpoint + 1
    with (tmp_path / "node.log").open("w") as log:
        process = subprocess.Popen([shutil.which("anvil"), "--host", "127.0.0.1", "--port", str(port),
            "--chain-id", "1", "--hardfork", "osaka", "--gas-limit", "30000000", "--silent"], stdout=log, stderr=log)
        try:
            for _ in range(100):
                if w3.is_connected():
                    break
                if process.poll() is not None:
                    raise AssertionError((tmp_path / "node.log").read_text())
                time.sleep(.05)
            else:
                raise AssertionError("isolated relay node did not start")
            key = "0x" + "00" * 31 + "01"
            sender = Account.from_key(key).address
            identity = tmp_path / "identity.json"
            identity.write_text(json.dumps(dict(private_key=key)))
            identity.chmod(0o600)
            w3.provider.make_request("anvil_setBalance", [sender, hex(10**24)])
            relay = deploy(w3, relay_artifact(network), *relay_constructor_args(policy))
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            for name, value in dict(SATOSHIFI_MAX_FEE_PER_GAS_WEI="3000000000",
                SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI="1000000000", SATOSHIFI_MAX_TX_GAS=str(2**24)).items():
                monkeypatch.setenv(name, value)
            def caller():
                # Each bootstrap/CLI/keeper entry point constructs a fresh sender;
                # no caller-local state or transaction journal is shared here.
                return DurableRelayBatch.for_rpc(identity, Web3(Web3.HTTPProvider(rpc)), relay.address, policy, 12)
            raw = lambda height: core.raw(height).hex()
            initial = [raw(first), raw(first + 1)]
            assert caller()(initial)["status"] == "CONFIRMING"
            assert w3.eth.get_transaction_count(sender) == 1
            tx_journal = Journal(str(identity) + ".relay-transactions.sqlite")
            signed = next(entry["result"] for entry in tx_journal.list() if entry["id"].endswith(":headers"))
            assert caller()([*initial, raw(first + 2)])["status"] == "PENDING"
            assert w3.eth.get_transaction_count(sender) == 1
            assert next(entry["result"] for entry in tx_journal.list() if entry["id"].endswith(":headers"))["tx_hash"] == signed["tx_hash"]
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            assert caller()([*initial, raw(first + 2)])["status"] == "PENDING"
            assert caller()([raw(first + 2)])["status"] == "CONFIRMING"
            assert w3.eth.get_transaction_count(sender) == 2
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            assert caller()([raw(first + 2)])["status"] == "VERIFIED"
            # Durable chunk written, then process dies before any signing. Another
            # operator completes it; recovery must not estimate/sign a duplicate.
            def crash(_):
                raise RuntimeError("crash before callback")
            with pytest.raises(RuntimeError):
                DurableRelayBatch(identity, 1, relay.address, policy, crash)([raw(first + 3)])
            outside = relay.functions.addBlockHeader(bytes.fromhex(raw(first + 3))).transact({"from": w3.eth.accounts[0]})
            assert w3.eth.wait_for_transaction_receipt(outside).status == 1
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            assert caller()([raw(first + 3), raw(first + 4)])["status"] == "PENDING"
            assert w3.eth.get_transaction_count(sender) == 2
            assert caller()([raw(first + 4)])["status"] == "CONFIRMING"
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            assert caller()([raw(first + 4)])["status"] == "VERIFIED"
            assert w3.eth.get_transaction_count(sender) == 3
            # Actual signed transaction waits in the mempool while a higher-fee
            # relayer publishes the same header first in the eventual block.
            w3.provider.make_request("evm_setAutomine", [False])
            original_wait = Eth.wait_for_transaction_receipt
            def no_wait(self, *args, **kwargs):
                raise TimeoutError("fixture leaves transaction pending")
            monkeypatch.setattr(Eth, "wait_for_transaction_receipt", no_wait)
            assert caller()([raw(first + 5)])["status"] == "BROADCAST"
            outside = relay.functions.addBlockHeader(bytes.fromhex(raw(first + 5))).transact({
                "from": w3.eth.accounts[0], "gas": 500000, "maxFeePerGas": 6000000000, "maxPriorityFeePerGas": 2000000000})
            monkeypatch.setattr(Eth, "wait_for_transaction_receipt", original_wait)
            w3.provider.make_request("evm_mine", [])
            assert w3.eth.get_transaction_receipt(outside).status == 1
            latest_entry = next(entry for entry in tx_journal.list() if entry["id"].endswith(":headers"))
            own_receipt = w3.eth.get_transaction_receipt(latest_entry["result"]["tx_hash"])
            assert own_receipt.status == 1
            assert w3.eth.get_transaction_receipt(outside).transactionIndex < own_receipt.transactionIndex
            w3.provider.make_request("evm_setAutomine", [True])
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            assert caller()([raw(first + 5)])["status"] == "VERIFIED"
            assert w3.eth.get_transaction_count(sender) == 4
            assert relay.functions.getMainchainHeight().call() == first + 5
            head = relay.functions.getMainchainHead().call()
            assert relay.functions.blocks(head).call()[2] == int(core.record(first + 5)["chainwork"], 16)
            # A dropped signed transaction still needs exact-byte recovery even
            # if another operator has caught up and Core's tip no longer grows.
            w3.provider.make_request("evm_setAutomine", [False])
            monkeypatch.setattr(Eth, "wait_for_transaction_receipt", no_wait)
            assert caller()([raw(first + 6)])["status"] == "BROADCAST"
            dropped = next(entry for entry in tx_journal.list() if entry["id"].endswith(":headers"))["result"]
            response = w3.provider.make_request("anvil_dropTransaction", ["0x" + dropped["tx_hash"].removeprefix("0x")])
            assert "error" not in response, response
            monkeypatch.setattr(Eth, "wait_for_transaction_receipt", original_wait)
            w3.provider.make_request("evm_setAutomine", [True])
            outside = relay.functions.addBlockHeader(bytes.fromhex(raw(first + 6))).transact({"from": w3.eth.accounts[0]})
            assert w3.eth.wait_for_transaction_receipt(outside).status == 1
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            core.height = first + 6
            def sync():
                return sync_relay(core, relay, policy, lambda _: pytest.fail("single-header fallback"),
                    send_headers=caller(), evm_confirmations=12, now=core.record(core.height)["time"] + 1)
            assert sync()["status"] == "IN_PROGRESS"
            assert w3.eth.get_transaction_count(sender) == 5
            assert w3.eth.get_transaction(dropped["tx_hash"]).hash.hex() == dropped["tx_hash"]
            assert w3.eth.get_transaction_receipt(dropped["tx_hash"]).status == 1
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            assert sync()["status"] == "MAINNET_RELAY_READY"
            assert caller().recover_pending() is None
            assert w3.eth.get_transaction_count(sender) == 5
            # The packaged permissionless keeper resumes the same journal with
            # only a gas-paying identity, policy, relay address and runtime pin.
            # Historical Core is an explicit fixture, never live-mainnet proof.
            from signer_agent import relay_keeper
            policy_file = tmp_path / "policy.json"
            policy_file.write_text(json.dumps(policy))
            args = SimpleNamespace(bitcoin_policy=str(policy_file), rpc=rpc, relay=relay.address,
                relay_code_hash=Web3.keccak(w3.eth.get_code(relay.address)).hex(), chain_id=1,
                evm_confirmations=12, bitcoin_rpc="fixture", bitcoin_cookie="fixture",
                identity_file=str(identity))
            monkeypatch.setattr(relay_keeper, "BitcoinRPC", lambda *a, **kw: core)
            monkeypatch.setattr(relay_keeper, "sync_relay", lambda *a, **kw: sync_relay(
                *a, **kw, now=core.record(core.height)["time"] + 1))
            core.height += 1
            keeper = relay_keeper.RelayKeeper(args)
            assert keeper.tick()["status"] == "IN_PROGRESS"
            assert w3.eth.get_transaction_count(sender) == 6
            keeper = relay_keeper.RelayKeeper(args)  # restart while receipt is unfinalized
            assert keeper.tick()["status"] == "IN_PROGRESS"
            assert w3.eth.get_transaction_count(sender) == 6
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            assert keeper.tick()["status"] == "MAINNET_RELAY_READY"
            assert w3.eth.get_transaction_count(sender) == 6
            args.relay_code_hash = "22" * 32
            with pytest.raises(ValueError, match="RELAY_RUNTIME_PIN_MISMATCH"):
                relay_keeper.RelayKeeper(args).tick()
            assert w3.eth.get_transaction_count(sender) == 6
        finally:
            process.terminate()
            process.wait(timeout=10)

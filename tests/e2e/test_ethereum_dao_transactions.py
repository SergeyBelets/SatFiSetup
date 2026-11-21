"""Actual DAO wallet transactions on isolated supported EVM networks.

This targets the durable transaction executor; the full bootstrap E2E separately
verifies the discovered DAO graph, first DKG and all provenance bindings.
"""
import json
import shutil
import socket
import subprocess
import time

from eth_account import Account
import pytest
from web3 import Web3
from web3.exceptions import Web3RPCError

from signer_agent.dao_client import DAOClient
from signer_agent.operations import Journal
from test_dkg_processes import deploy


@pytest.mark.parametrize("chain", [1, 11155111, 31337], ids=["ethereum", "sepolia", "local"])
@pytest.mark.parametrize("amount", [1, 10**18])
@pytest.mark.parametrize("midnight", [False, True], ids=["next-slot", "utc-midnight"])
def test_ethereum_dao_approve_stake_unstake_and_idempotence(tmp_path, monkeypatch, record_property, amount, midnight, chain):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    rpc = f"http://127.0.0.1:{port}"
    w3 = Web3(Web3.HTTPProvider(rpc))
    log_path = tmp_path / "ethereum-dao-node.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([shutil.which("anvil"), "--host", "127.0.0.1", "--port", str(port),
            "--chain-id", str(chain), "--hardfork", "osaka", "--gas-limit", "30000000",
            "--timestamp", "1200001000", "--silent"], stdout=log, stderr=log)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError(log_path.read_text())
                if w3.is_connected():
                    break
                time.sleep(.05)
            else:
                raise AssertionError("isolated Ethereum node did not start")
            assert w3.eth.chain_id == chain
            key = "0x" + "00" * 31 + "01"
            sender = Account.from_key(key).address
            stake_token = deploy(w3, "CustodianDAOToken", "DAO fixture", "DAO", sender)
            reward_token = deploy(w3, "StageRewardToken")
            staking = deploy(w3, "CustodianDAOStaking", stake_token.address,
                reward_token.address, w3.eth.accounts[2])
            w3.provider.make_request("anvil_setBalance", [sender, hex(10**24)])
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            # Control inclusion and subsequent confirmation blocks explicitly;
            # Anvil's asynchronous autominer can otherwise race the timestamp.
            assert "error" not in w3.provider.make_request("evm_setAutomine", [False])
            for name, value in dict(SATOSHIFI_MAX_FEE_PER_GAS_WEI="3000000000",
                SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI="1000000000",
                SATOSHIFI_MAX_TX_GAS=str(2**24)).items():
                monkeypatch.setenv(name, value)
            original = Web3.HTTPProvider.make_request
            inclusion_timestamp = None
            reject_pending = False
            gas_requests = []

            def mine_after_send(provider, method, params):
                if method == "eth_estimateGas":
                    gas_requests.append(params[1] if len(params) > 1 else None)
                    if reject_pending and len(params) > 1 and params[1] == "pending":
                        return dict(jsonrpc="2.0", id=0, error=dict(code=-32000,
                            message="fixture pending estimate unavailable"))
                if method == "eth_sendRawTransaction":
                    # Deliberately change the inclusion timestamp AFTER all
                    # estimates. A stale pending view cannot predict crossing
                    # into a new checkpoint/day, as in the failed full V5 run.
                    assert inclusion_timestamp is not None
                    assert "error" not in original(provider, "evm_setNextBlockTimestamp", [inclusion_timestamp])
                response = original(provider, method, params)
                if method == "eth_sendRawTransaction" and "result" in response:
                    assert original(provider, "eth_chainId", [])["result"] == hex(chain)
                    assert "error" not in original(provider, "anvil_mine", ["0x80", "0x0"])
                return response

            monkeypatch.setattr(Web3.HTTPProvider, "make_request", mine_after_send)
            client = DAOClient(rpc, str(chain))
            client.contracts = dict(token=stake_token, staking=staking)
            client.config = dict(chain_id=str(chain), factory=staking.address,
                confederation_id="isolated-dao-wallet", confirmations="12" if chain == 1 else "1", contracts={
                    name: dict(address=contract.address, artifact=kind,
                        runtime_hash=Web3.keccak(w3.eth.get_code(contract.address)).hex())
                    for name, contract, kind in (("token", stake_token, "CustodianDAOToken"),
                                                ("staking", staking, "CustodianDAOStaking"))})
            identity, state = tmp_path / "identity.json", tmp_path / "dao-wallet.sqlite"
            identity.write_text(json.dumps(dict(private_key=key)))
            identity.chmod(0o600)
            if chain == 1:
                reject_pending = True
                before = w3.eth.get_transaction_count(sender)
                with pytest.raises(Web3RPCError, match="fixture pending estimate unavailable"):
                    client.transact("token", "approve", [staking.address, amount], sender,
                        identity, True, state, "pending-unavailable")
                assert w3.eth.get_transaction_count(sender) == before
                assert gas_requests == ["pending"]  # No latest fallback.
                assert "raw_transaction" not in Journal(state).get("dao-tx-pending-unavailable:send")["result"]
                reject_pending = False
            evidence = []
            stake_time = None
            for name, method, args in (("token", "approve", [staking.address, amount]),
                                       ("staking", "stake", [amount]),
                                       ("staking", "unstake", [amount])):
                latest = w3.eth.get_block("latest").timestamp
                inclusion_timestamp = latest + 12
                if method == "stake" and midnight:
                    inclusion_timestamp = (latest // 86400 + 1) * 86400 - 12
                if method == "unstake":
                    assert latest == stake_time
                    if midnight:
                        assert inclusion_timestamp % 86400 == 0
                        assert staking.functions.eligibleStake(sender, inclusion_timestamp // 86400).call() == amount
                    if chain == 1:
                        estimate = staking.functions.unstake(amount).estimate_gas({"from": sender})
                        before = w3.eth.get_transaction_count(sender)
                        monkeypatch.setenv("SATOSHIFI_MAX_TX_GAS", str(estimate))
                        with pytest.raises(ValueError, match="^ETHEREUM_TRANSACTION_GAS_LIMIT$"):
                            client.transact(name, method, args, sender, identity, True, state, "too-small")
                        assert w3.eth.get_transaction_count(sender) == before
                        assert "raw_transaction" not in Journal(state).get("dao-tx-too-small:send")["result"]
                        monkeypatch.setenv("SATOSHIFI_MAX_TX_GAS", str(2**24))
                result = client.transact(name, method, args, sender, identity, True, state, method)
                assert result["status"] == "VERIFIED", result
                nonce = w3.eth.get_transaction_count(sender)
                repeated = client.transact(name, method, args, sender, identity, True, state, method)
                assert repeated["tx_hash"] == result["tx_hash"]
                assert w3.eth.get_transaction_count(sender) == nonce
                transaction = w3.eth.get_transaction(result["tx_hash"])
                receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
                assert receipt.status == 1 and transaction["type"] == 2
                assert transaction.gas <= 2**24
                assert receipt.blockNumber <= w3.eth.get_block("finalized").number
                mined_time = w3.eth.get_block(receipt.blockNumber).timestamp
                assert mined_time == inclusion_timestamp
                if method == "stake":
                    stake_time = mined_time
                evidence.append(dict(method=method, gas=transaction.gas, gas_used=receipt.gasUsed,
                    block_timestamp=mined_time, hash=result["tx_hash"]))
            assert staking.functions.stakedBalance(sender).call() == 0
            assert stake_token.functions.balanceOf(sender).call() == 10_000_000 * 10**18
            record_property("ethereum_dao_transactions", json.dumps(dict(chain_id=chain,
                live_mainnet_transactions=False, real_dao_staking=True, idempotent_retries=True,
                amount=str(amount), stale_pending_then_utc_midnight=midnight,
                pending_estimate_failure_rejected_before_signing=chain == 1,
                insufficient_gas_cap_rejected_before_signing=chain == 1, transactions=evidence)))
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

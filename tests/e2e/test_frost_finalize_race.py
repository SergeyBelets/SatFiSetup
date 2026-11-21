"""Exercise the durable nonce lane after two valid finalizers race on Anvil."""

import json
import subprocess
import sys

import pytest
from coincurve import PrivateKey
from eth_account import Account
from web3 import Web3

from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_parallel import save_signed
from signer_agent.bootstrap_recovery import identity_lane
from signer_agent.operations import canonical
from test_dkg_processes import RPC, deploy, tx


@pytest.mark.parametrize("legacy_lane", [False, True], ids=["bound", "legacy"])
def test_losing_finalizer_can_continue_on_the_same_nonce_lane(tmp_path, legacy_lane):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337, "requires an isolated Anvil test chain"
    verifier = deploy(w3, "FROSTVerifier")
    coordinator = deploy(w3, "FROSTCoordinator", verifier.address)
    tx(w3, coordinator.functions.grantRole(coordinator.functions.SESSION_CREATOR_ROLE().call(), w3.eth.accounts[0]))
    winner, loser = Account.create(), Account.create()
    for account in (winner, loser):
        assert "error" not in w3.provider.make_request("anvil_setBalance", [account.address, hex(10**20)])

    identity = tmp_path / "loser.json"
    identity.write_text(json.dumps(dict(private_key=loser.key.hex())))
    identity.chmod(0o600)
    _, _, lane = identity_lane(identity)
    transaction_journal = tmp_path / "signer.sqlite"

    # This test targets EVM finalization and nonce persistence. The verifier
    # checks a real BIP340 aggregate from a local test key without a DKG run.
    key = PrivateKey()
    message = Web3.keccak(text="Anvil finalize race")
    signature = key.sign_schnorr(message)
    deadline = w3.eth.get_block("latest").timestamp + 600
    for session in (41, 42):
        tx(w3, coordinator.functions.createSessionWithId(session, key.public_key_xonly.format(),
            [winner.address, loser.address], 2, deadline))

    def runner(session):
        values = Bootstrap.wire([session, signature, message])
        deployment = "signer-" + Web3.keccak(text=canonical([coordinator.address, "finalizeSession", values])).hex()
        step = dict(id="send", kind="call", contract="target", method="finalizeSession", args=values)
        profile = dict(deployment_id=deployment, chain_id="31337", evm_rpc=RPC, mode="integration",
                       bitcoin_network="regtest", contracts=dict(target=dict(address=coordinator.address,
                       artifact="FROSTCoordinator", runtime_hash=Web3.keccak(w3.eth.get_code(coordinator.address)).hex())),
                       steps=[step])
        return Bootstrap(profile, transaction_journal), step

    r, step = runner(41)
    op = r.prefix + ":send"
    r.journal.put(op, step)
    gas_price = w3.eth.gas_price
    nonce = w3.eth.get_transaction_count(loser.address)

    def signed(account, gas_price):
        call = coordinator.functions.finalizeSession(41, signature, message)
        call.call({"from": account.address})
        gas = call.estimate_gas({"from": account.address}) * 2
        transaction = call.build_transaction({
            "from": account.address, "chainId": w3.eth.chain_id,
            "nonce": w3.eth.get_transaction_count(account.address), "gas": gas, "gasPrice": gas_price})
        return account.sign_transaction(transaction)

    first = signed(winner, gas_price * 4)
    second = signed(loser, gas_price * 2)
    saved = dict(tx_hash=second.hash.hex(), raw_transaction=second.raw_transaction.hex(), nonce=str(nonce))
    if legacy_lane:
        lane.put(saved["tx_hash"], dict(chain_id="31337", tx_hash=saved["tx_hash"],
            raw_transaction=saved["raw_transaction"]))
    else:
        save_signed(lane, r, op, step, saved)
    assert "error" not in w3.provider.make_request("evm_setAutomine", [False])
    try:
        w3.eth.send_raw_transaction(first.raw_transaction)
        w3.eth.send_raw_transaction(second.raw_transaction)
        assert "error" not in w3.provider.make_request("evm_mine", [])
        good = w3.eth.get_transaction_receipt(first.hash)
        bad = w3.eth.get_transaction_receipt(second.hash)
        assert good.blockHash == bad.blockHash
        assert (good.status, bad.status) == (1, 0)
    finally:
        assert "error" not in w3.provider.make_request("evm_setAutomine", [True])

    r.journal.update(op, "REVERTED", dict(saved, block_number=str(bad.blockNumber), block_hash=bad.blockHash.hex()))
    if legacy_lane:
        binding = subprocess.run([sys.executable, "-m", "signer_agent.cli", "bootstrap", "bind-legacy-nonce",
            "--identity-file", str(identity), "--rpc", RPC, "--tx-hash", saved["tx_hash"],
            "--source-journal", str(transaction_journal)], capture_output=True, text=True, timeout=30, check=True)
        assert json.loads(binding.stdout)["status"] == "EVM_LEGACY_NONCE_BOUND"
    next_runner, _ = runner(42)
    assert next_runner.apply_confirmed(identity)["status"] == "VERIFIED"
    assert coordinator.functions.isFinalized(42).call()
    assert next_runner.journal.get(next_runner.prefix + ":send")["result"]["nonce"] == str(nonce + 1)
    assert lane.get(second.hash.hex())["result"]["receipt_status"] == "0"
    assert r.journal.get(op)["status"] == "REVERTED"

    acknowledgement = subprocess.run([sys.executable, "-m", "signer_agent.cli", "bootstrap", "acknowledge-revert",
        "--identity-file", str(identity), "--rpc", RPC, "--tx-hash", second.hash.hex()], capture_output=True,
        text=True, timeout=30, check=True)
    assert json.loads(acknowledgement.stdout)["status"] == "REVERT_ACKNOWLEDGED"

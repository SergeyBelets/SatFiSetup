"""Confirm terminal DKG state before recovering an unused factory ID."""

import json
import subprocess
import sys
import time

import pytest
from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError

from signer_agent.bootstrap_formation import (
    confederation_formation_hash,
    federation_formation_hash,
    share_token_hash,
)
from signer_agent.operations import Journal
from test_dkg_processes import RPC, artifact, tx


@pytest.mark.parametrize(("kind", "terminal_phase", "cancel"), [
    ("federation", 6, False), ("confederation", 5, False), ("federation", 6, True),
])
def test_operator_transfers_only_a_confirmed_unused_reservation(tmp_path, kind, terminal_phase, cancel):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337
    key = (1).to_bytes(32, "big")  # Fixture identity funded only on local Anvil.
    operator = Account.from_key(key).address
    w3.provider.make_request("anvil_setBalance", [operator, hex(10**22)])
    w3.provider.make_request("anvil_impersonateAccount", [operator])
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"private_key": key.hex()}))
    identity.chmod(0o600)

    def deploy(name, *values):
        compiled = artifact(name)
        contract = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"]["object"])
        receipt = tx(w3, contract.constructor(*values), sender=operator)
        return w3.eth.contract(address=receipt.contractAddress, abi=compiled["abi"])

    registry = deploy("DKGCoordinator")
    verifier = deploy("FROSTVerifier")
    coordinator = deploy("FROSTCoordinator", verifier.address)
    tx(w3, coordinator.functions.bindDKGRegistry(registry.address), sender=operator)
    # This fixture stops before adoption and needs only the factory and DKG graph.
    federation = deploy("FederationFactory", registry.address, verifier.address, coordinator.address)
    factory = federation if kind == "federation" else deploy(
        "ConfederationFactory", federation.address, registry.address, verifier.address, coordinator.address
    )
    title = kind.capitalize()
    reserve = getattr(factory.functions, f"reserve{title}Id")
    move = getattr(factory.functions, f"transfer{title}Reservation")
    reserved_session = getattr(factory.functions, f"reserved{title}Session")
    reserved_terms = getattr(factory.functions, f"reserved{title}Terms")
    members = [operator, w3.eth.accounts[1]]
    old, replacement = (Web3.keccak(text=f"{kind}-{label}") for label in ("old", "new"))
    identifier = f"recovery-{kind}"
    formation = (federation_formation_hash(members, 2) if kind == "federation"
                 else confederation_formation_hash(2, ["founder-federation"],
                     [(f"custodian-{i}", member, b"\x02", True) for i, member in enumerate(members)], 2, 2))
    token = share_token_hash("Original Shares", "OS")

    def create(session, epoch):
        keys = [Web3.keccak(text=f"{session.hex()}:{member}") for member in members]
        tx(w3, registry.functions.createCeremony(session, members, 2, keys, epoch,
            Web3.keccak(text="EVM_AUTH"), registry.functions.PROTOCOL_V1().call()), sender=operator)

    def mine():
        assert "error" not in w3.provider.make_request("evm_mine", [])

    create(old, 1)
    tx(w3, reserve(old, identifier, formation, token), sender=operator)
    deadline = registry.functions.getSessionInfo(old).call()[5]
    command = [sys.executable, "-m", "signer_agent.cli", "setup", "reservation-transfer",
               "--kind", kind, "--factory", factory.address, "--entity-id", identifier,
               "--from-session", Web3.to_hex(old), "--to-session", Web3.to_hex(replacement),
               "--rpc", RPC, "--chain-id", "31337", "--confirmations", "2"]

    if terminal_phase == 6:
        create(replacement, 2)
        mine()
        before = subprocess.run(command, capture_output=True, text=True, timeout=20)
        assert "RESERVATION_TRANSFER_SOURCE_NOT_TERMINAL" in before.stdout + before.stderr
        tx(w3, registry.functions.abortCeremony(old, Web3.keccak(text="local test")), sender=operator)
        too_soon = subprocess.run(command, capture_output=True, text=True, timeout=20)
        assert "RESERVATION_TRANSFER_SOURCE_NOT_TERMINAL" in too_soon.stdout + too_soon.stderr
    else:
        assert "error" not in w3.provider.make_request("evm_setNextBlockTimestamp", [deadline + 1])
        mine()
        create(replacement, 2)
        mine()
        before = subprocess.run(command, capture_output=True, text=True, timeout=20)
        assert "RESERVATION_TRANSFER_SOURCE_NOT_TERMINAL" in before.stdout + before.stderr
        tx(w3, registry.functions.advancePhase(old), sender=w3.eth.accounts[1])

    assert registry.functions.getSessionInfo(old).call()[4] == terminal_phase
    mine()
    with pytest.raises(ContractLogicError):
        move(identifier, old, replacement).call({"from": w3.eth.accounts[1]})
    preview = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    reviewed = json.loads(preview.stdout)
    assert reviewed["status"] == "RESERVATION_TRANSFER_REVIEW"
    assert reviewed["review"]["source_status"] == ("Aborted" if terminal_phase == 6 else "Failed")
    assert bytes(reserved_session(identifier).call()) == old

    state = tmp_path / "transactions.sqlite"
    send = command + ["--send", "--accept-fingerprint", reviewed["fingerprint"],
                      "--identity-file", str(identity), "--state", str(state)]
    nonce = w3.eth.get_transaction_count(operator)
    if cancel:
        cancel_pending_transfer(w3, send, state, identity, operator, nonce, mine)
        assert bytes(reserved_session(identifier).call()) == old
        assert tuple(map(bytes, reserved_terms(identifier).call())) == (formation, token)
        return
    with subprocess.Popen(send, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        try:
            until = time.monotonic() + 40
            while child.poll() is None and time.monotonic() < until:
                if w3.eth.get_transaction_count(operator) > nonce:
                    mine()
                    break
                time.sleep(0.2)
            stdout, stderr = child.communicate(timeout=45)
        except BaseException:
            child.kill()
            raise
    assert child.returncode == 0, stdout + stderr
    result = json.loads(stdout)
    assert result["status"] == "RESERVATION_TRANSFERRED"
    assert bytes(reserved_session(identifier).call()) == replacement
    assert tuple(map(bytes, reserved_terms(identifier).call())) == (formation, token)
    repeat = subprocess.run(send, capture_output=True, text=True, timeout=30)
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert json.loads(repeat.stdout)["tx_hash"] == result["tx_hash"]
    assert w3.eth.get_transaction_count(operator) == nonce + 1

    with pytest.raises(ContractLogicError):
        reserve(old, "other-id", formation, token).call({"from": operator})
    with pytest.raises(ContractLogicError):
        move(identifier, old, replacement).call({"from": operator})


def cancel_pending_transfer(w3, send, state, identity, operator, nonce, mine):
    assert "error" not in w3.provider.make_request("evm_setAutomine", [False])
    try:
        with subprocess.Popen(send, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            try:
                until = time.monotonic() + 40
                while child.poll() is None and time.monotonic() < until:
                    if w3.eth.get_transaction_count(operator, "pending") == nonce + 1:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("transfer never reached the local mempool")
            finally:
                if child.poll() is None:
                    child.terminate()
                child.communicate(timeout=12)
        pending, = [row["result"]["tx_hash"] for row in Journal(state).list()
                    if row["id"].endswith(":transfer") and row["result"].get("tx_hash")]
        old = w3.eth.get_transaction(pending)
        cap = int(old["maxFeePerGas"]) * 3 + 100
        tip = int(old["maxPriorityFeePerGas"]) * 2 + 1
        command = [sys.executable, "-m", "signer_agent.cli", "bootstrap", "replace-pending",
                   "--identity-file", str(identity), "--rpc", RPC, "--tx-hash", pending,
                   "--max-fee-per-gas-wei", str(cap), "--max-priority-fee-per-gas-wei", str(tip),
                   "--max-cost-wei", str(cap * 21000), "--cancel-reservation"]

        def checked(cmd):
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        review = checked(command)
        assert review["status"] == "EVM_REPLACEMENT_REVIEW"
        cancellation = checked(command + ["--confirm-replacement", review["fingerprint"]])
        assert cancellation["status"] == "EVM_REPLACEMENT_BROADCAST"
        mine()
        assert checked(command)["status"] == "RESERVATION_TRANSFER_CANCEL_CONFIRMING"
        mine()
        done = checked(command)
        assert done["status"] == "RESERVATION_TRANSFER_CANCELLED" and done["tx_hash"] == cancellation["tx_hash"]
        assert checked(send)["status"] == "RESERVATION_TRANSFER_CANCELLED"
        assert w3.eth.get_transaction_count(operator) == nonce + 1
    finally:
        w3.provider.make_request("evm_setAutomine", [True])

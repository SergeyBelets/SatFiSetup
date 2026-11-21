"""Local Py-EVM receipts, independently signed transactions and real SQLite journals."""
import fcntl
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from eth_account import Account
from eth_tester import EthereumTester, PyEVMBackend
from web3 import Web3, EthereumTesterProvider

from signer_agent import bootstrap_recovery, finalize_recovery
from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_parallel import recover_identity, save_signed
from signer_agent.coordinator import CoordinatorClient, Session
from signer_agent.operations import Journal
from signer_agent.transaction_wait import TransactionPending, TransactionRecoveryRequired


def run_case(artifact, directory, case):
    backend = PyEVMBackend()
    tester = EthereumTester(backend)
    w3 = Web3(EthereumTesterProvider(tester))
    winner = w3.eth.accounts[1]
    loser = Account.from_key(backend.account_keys[2].to_bytes())
    message = Web3.keccak(text="isolated duplicate-finalize acceptance")
    sig = bytes([7]) * 64
    own_sig = bytes([8]) * 64 if case == "different_signature" else sig
    factory = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["evm"]["bytecode"]["object"])
    deployed = w3.eth.wait_for_transaction_receipt(factory.constructor(message).transact({"from": w3.eth.accounts[0]}))
    contract = w3.eth.contract(address=deployed.contractAddress, abi=artifact["abi"])
    identity = directory / "custodian.json"
    identity.write_text(json.dumps({"private_key": loser.key.hex()}))
    identity.chmod(0o600)
    source = Journal(str(identity) + ".transactions.sqlite")
    _, _, lane = bootstrap_recovery.identity_lane(identity)
    args = Bootstrap.wire([42, own_sig, message])
    prefix = finalize_recovery._operation(contract.address, args)
    step = dict(id="send", kind="call", contract="target", method="finalizeSession", args=args)
    profile = dict(deployment_id=prefix, chain_id=str(w3.eth.chain_id), evm_confirmations="2",
                   steps=[step], contracts={"target": dict(address=contract.address, artifact="FROSTCoordinator",
                   runtime_hash=Web3.keccak(w3.eth.get_code(contract.address)).hex())})
    source.put(prefix, {"version": "1", "profile": profile})
    key = prefix + ":send"
    source.put(key, step)
    receipts = {}
    runner = SimpleNamespace(w3=w3, journal=source, prefix=prefix, evm_confirmations=2)
    client = SimpleNamespace(address=contract.address, contract=contract, identity_file=identity,
                             transaction_journal=str(source.path), chain_id=w3.eth.chain_id,
                             sender=loser.address, w3=w3, _confirmation_graph=lambda: {"evm_confirmations": "2"})

    def submit_pair(own_call):
        # The coordinator preflight has passed. This callback chooses deterministic block order.
        nonce = w3.eth.get_transaction_count(loser.address)
        tx = own_call.build_transaction(dict(chainId=w3.eth.chain_id, nonce=nonce, gas=300000, type=2,
            maxPriorityFeePerGas=10**9, maxFeePerGas=4 * 10**9, **{"from": loser.address}))
        raw = loser.sign_transaction(tx).raw_transaction
        h = Web3.keccak(raw).hex()
        envelope = dict(raw_transaction=raw.hex(), nonce=str(nonce), tx_hash=h)
        save_signed(lane, runner, key, step, envelope)
        if case == "same_block":
            tester.disable_auto_mine_transactions()
        first = contract.functions.finalizeSession(42, sig, message).transact({"from": winner, "gas": 300000})
        second = w3.eth.send_raw_transaction(raw)
        if case == "same_block":
            tester.mine_blocks(1)
        for name, digest in (("winner", first), ("duplicate", second)):
            receipts[name] = w3.eth.get_transaction_receipt(digest)
        assert receipts["winner"].status == 1 and receipts["duplicate"].status == 0
        assert w3.eth.get_transaction_count(loser.address) == nonce + 1
        assert bytes(contract.functions.getResult(42).call()[0]) == sig
        if case == "same_block":
            assert receipts["winner"].blockHash == receipts["duplicate"].blockHash
            assert receipts["winner"].transactionIndex < receipts["duplicate"].transactionIndex
        else:
            assert receipts["winner"].blockNumber < receipts["duplicate"].blockNumber
        source.update(key, "REVERTED", dict(envelope, block_number=str(receipts["duplicate"].blockNumber),
                      block_hash=receipts["duplicate"].blockHash.hex()))
        raise TransactionRecoveryRequired(dict(reason="TRANSACTION_REVERTED", tx_hash=h))

    client._send = submit_pair
    try:
        CoordinatorClient.finalize(client, 42, own_sig, message)
    except TransactionPending as ex:
        assert case != "different_signature" and ex.result["reason"] == "FINALIZATION_AWAITING_FINALITY"
    except TransactionRecoveryRequired:
        assert case == "different_signature"
    else:
        raise AssertionError("insufficient two-block depth should not acknowledge the receipt")
    duplicate = receipts["duplicate"]
    assert lane.get(duplicate.transactionHash.hex())["status"] != "CONFIRMED"
    tester.mine_blocks(2)
    # Recreate the read-side client as on process restart; no cached contract or journal data.
    resumed = SimpleNamespace(**vars(client))
    resumed.contract = w3.eth.contract(address=contract.address, abi=artifact["abi"])
    resumed._send = lambda call: (_ for _ in ()).throw(AssertionError("unexpected replacement or duplicate send"))
    original_nonce = w3.eth.get_transaction_count(loser.address)
    def check_terminal():
        finalize_recovery.reconcile_terminal(resumed, Session.from_contract(resumed.contract.functions.getSession(42).call()))

    def check_outcome(observe):
        try:
            observe()
        except TransactionRecoveryRequired as error:
            assert case == "different_signature" and error.result["tx_hash"] == duplicate.transactionHash.hex()
            assert error.result["reason"] == "FINALIZATION_REVERT_REQUIRES_REVIEW"
            return "RECOVERY_REQUIRED"
        assert case != "different_signature"
        return "FINALIZED"

    terminal_status = check_outcome(check_terminal)
    row = Journal(lane.path).get(duplicate.transactionHash.hex())
    if case == "different_signature":
        assert row["status"] != "CONFIRMED"
    else:
        assert row["status"] == "CONFIRMED" and row["result"]["receipt_status"] == "0"
        assert row["result"]["reason"] == "SESSION_ALREADY_FINALIZED"
    assert check_outcome(lambda: CoordinatorClient.finalize(resumed, 42, own_sig, message)) == terminal_status
    assert w3.eth.get_transaction_count(loser.address) == original_nonce
    assert source.get(key)["status"] == "REVERTED"
    followup = None
    if case != "different_signature":
        # A fresh, independently requested zero-value self-transfer uses the same SDK lane.
        next_source = Journal(directory / "next-transaction.sqlite")
        next_prefix = "next-local-operation"
        next_step = dict(id="send", kind="transfer", to=loser.address, value="0")
        next_key = next_prefix + ":send"
        next_runner = SimpleNamespace(w3=w3, journal=next_source, prefix=next_prefix, evm_confirmations=2)
        next_source.put(next_key, next_step)
        _, lock, same_lane = bootstrap_recovery.identity_lane(identity, require_existing=True)
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            floor = recover_identity(same_lane, next_runner, loser)
            nonce = w3.eth.get_transaction_count(loser.address, "pending")
            assert floor == original_nonce == nonce
            raw = loser.sign_transaction(dict(chainId=w3.eth.chain_id, nonce=nonce, type=2,
                to=loser.address, value=0, gas=21000, maxPriorityFeePerGas=10**9, maxFeePerGas=4*10**9)).raw_transaction
            next_hash = Web3.keccak(raw).hex()
            save_signed(same_lane, next_runner, next_key, next_step,
                        dict(tx_hash=next_hash, raw_transaction=raw.hex(), nonce=str(nonce)))
            w3.eth.send_raw_transaction(raw)
        tester.mine_blocks(2)
        next_receipt = w3.eth.get_transaction_receipt(next_hash)
        assert next_receipt.status == 1 and w3.eth.get_transaction(next_hash).nonce == nonce
        assert w3.eth.get_transaction_count(loser.address) == nonce + 1
        followup = dict(status=next_receipt.status, nonce=nonce, block=next_receipt.blockNumber,
                        tx_hash=next_hash, gas=next_receipt.gasUsed)
    def detail(receipt):
        return dict(status=receipt.status, block=receipt.blockNumber, tx_index=receipt.transactionIndex,
                    gas=receipt.gasUsed, tx_hash=receipt.transactionHash.hex())
    return dict(case=case, chain_id=w3.eth.chain_id, winner=detail(receipts["winner"]), duplicate=detail(duplicate),
                journal=row["status"], source=source.get(key)["status"], released=row["result"].get("reason"),
                additional_recovery_nonces=0, terminal_status=terminal_status, next_independent_transaction=followup)


if __name__ == "__main__":
    artifact = json.loads(subprocess.check_output(["node", str(Path(__file__).with_name("compile.cjs"))]))
    for case_name in ("same_block", "previous_block", "different_signature"):
        with TemporaryDirectory() as work:
            print(json.dumps(run_case(artifact, Path(work), case_name), sort_keys=True), flush=True)

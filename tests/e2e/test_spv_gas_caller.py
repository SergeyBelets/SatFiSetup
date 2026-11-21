"""Bounded automatic claims on isolated Anvil; authority/SPV fixtures are synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys

from eth_account import Account
import pytest
from web3 import Web3

from signer_agent.bootstrap import artifact
from signer_agent.gas_caller import GasCaller, ClaimRunner


def test_bounded_claims_receipts_and_recovery(tmp_path, monkeypatch):
    rpc = os.environ.get("SATOSHIFI_GAS_E2E_RPC")
    fixture_out = os.environ.get("SATOSHIFI_GAS_FIXTURE_OUT")
    caller_out = os.environ.get("SATOSHIFI_GAS_CALLER_FIXTURE_OUT")
    if not rpc or not fixture_out or not caller_out:
        pytest.skip("explicit isolated Anvil and private fixture artifacts required")
    w3 = Web3(Web3.HTTPProvider(rpc))
    assert w3.eth.chain_id == 31337
    assert "error" not in w3.provider.make_request("anvil_reset", [])
    tomorrow = (w3.eth.get_block("latest").timestamp // 86400 + 1) * 86400 + 1000
    w3.provider.make_request("evm_setNextBlockTimestamp", [tomorrow])
    w3.provider.make_request("evm_mine", [])
    sender = w3.eth.accounts[0]
    receipts = []

    def record(tx, label, success=True):
        receipt = w3.eth.wait_for_transaction_receipt(tx)
        assert receipt.status == int(success), label
        assert receipt.gasUsed <= 2**24
        assert w3.eth.get_block(receipt.blockNumber).hash == receipt.blockHash
        receipts.append(dict(label=label, transaction_hash=Web3.to_hex(receipt.transactionHash),
            block_number=receipt.blockNumber, block_hash=Web3.to_hex(receipt.blockHash),
            status=receipt.status, gas_used=receipt.gasUsed,
            effective_gas_price=receipt.effectiveGasPrice))
        return receipt

    def deploy(name, args=(), fixture=False, availability=False):
        compiled = (json.loads((Path(caller_out) / "GasCallerFixture.sol" / (name + ".json")).read_text())
            if availability else json.loads((Path(fixture_out) / "SPVServicePayment.t.sol" / (name + ".json")).read_text())
            if fixture else artifact(name))
        factory = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"]["object"])
        receipt = record(factory.constructor(*args).transact({"from": sender, "gas": 2**24}), "deploy:" + name)
        return w3.eth.contract(address=receipt.contractAddress, abi=compiled["abi"]), receipt

    def call(fn, label):
        return record(fn.transact({"from": sender, "gas": 2**24}), label)

    caller, alice = [Account.from_key(value.to_bytes(32, "big")) for value in (2701, 2702)]
    identity = tmp_path / "caller.json"
    identity.write_text(json.dumps(dict(private_key=Web3.to_hex(caller.key))))
    identity.chmod(0o600)
    record(w3.eth.send_transaction({"from": sender, "to": caller.address, "value": 10**18}), "seed caller reserve")
    receiver, _ = deploy("GasCallerAvailabilityFixture", availability=True)
    verifier, _ = deploy("GasVerifierFixture", fixture=True)
    coordinator, _ = deploy("GasCoordinatorFixture", [verifier.address], fixture=True)
    auth, _ = deploy("GasAuthorizationFixture", [coordinator.address, [alice.address]], fixture=True)
    factory, _ = deploy("GasConfederationFixture", [auth.address], fixture=True)
    state, _ = deploy("GasStateFixture", fixture=True)
    registry, creation = deploy("SPVServicePaymentRegistry", [factory.address, "conf", state.address])
    binding = dict(schema="satoshifi.spv-gas-binding.v1", chain_id="31337", evm_confirmations="1",
        registry=registry.address, registry_creation_block=str(creation.blockNumber),
        registry_runtime_hash=Web3.to_hex(Web3.keccak(w3.eth.get_code(registry.address))),
        factory=factory.address, confederation_id="conf", state_view=state.address,
        authorization=auth.address, domain=Web3.to_hex(registry.functions.domain().call()))

    def route(epoch, payees, amount, floor=10**15):
        receipt = call(registry.functions.createTreasury(epoch, payees, [1] * len(payees)), "create treasury")
        address = registry.events.TreasuryCreated().process_receipt(receipt)[0]["args"]["treasury"]
        treasury = w3.eth.contract(address=address, abi=artifact("ConfederationGasTreasury")["abi"])
        # Donations suffice for claims; never assert they are a paid service job.
        record(w3.eth.send_transaction({"from": sender, "to": address, "value": amount}), "donation")
        return treasury, dict(binding=binding, treasury=address, epoch=str(epoch),
            roster_hash=Web3.to_hex(treasury.functions.rosterHash().call()),
            payees=[dict(address=payee, balance_floor_wei=str(floor), min_claim_wei=str(10**14)) for payee in payees])

    treasury, first = route(1, [receiver.address, alice.address], 6 * 10**15)
    # Claiming a proposed canonical treasury needs no state availability or activation.
    config = dict(schema="satoshifi.spv-gas-caller.v1", chain_id="31337", evm_confirmations="1",
        budget=dict(period_seconds="86400", max_period_cost_wei=str(10**15),
            max_transactions_per_period="4", max_attempts_per_payee_epoch="3", retry_backoff_seconds="600",
            gas_limit="150000", max_fee_per_gas_wei="1000000000", max_priority_fee_per_gas_wei="1000000",
            min_caller_balance_wei=str(10**15), min_claim_to_fee_ratio="1"), routes=[first])
    worker = GasCaller(w3, config, identity)
    build = ClaimRunner.build_transaction

    def availability_changes_after_successful_simulation(self, step, account, nonce):
        tx = build(self, step, account, nonce)
        call(receiver.functions.setAvailable(False), "receiver becomes unavailable after simulation")
        return tx

    monkeypatch.setattr(ClaimRunner, "build_transaction", availability_changes_after_successful_simulation)
    failed = worker.tick()
    assert failed["status"] == "CLAIM_REVERTED" and 0 < int(failed["cost_wei"]) <= worker.budget.maximum
    assert failed["recipient"]["balance_wei"] == "0"
    assert treasury.functions.released(receiver.address).call() == 0
    record(failed["transaction_hash"], "bounded ordinary availability failure", success=False)
    transaction = w3.eth.get_transaction(failed["transaction_hash"])
    assert transaction.gas == 150000 and transaction.maxFeePerGas == 10**9
    nonce_entry = worker.identity_journal.get(transaction.hash.hex())
    assert nonce_entry["status"] == "CONFIRMED" and nonce_entry["result"]["receipt_status"] == "0"
    monkeypatch.setattr(ClaimRunner, "build_transaction", build)
    worker = GasCaller(w3, config, identity)
    paid = worker.tick()
    assert paid["status"] == "CLAIM_CONFIRMED" and paid["claimed_wei"] == str(3 * 10**15)
    assert paid["recipient"]["status"] == "FUNDED"
    record(paid["transaction_hash"], "next payee funded despite previous revert")
    assert w3.eth.get_balance(alice.address) == 3 * 10**15
    nonce = w3.eth.get_transaction_count(caller.address)
    idle = worker.tick()
    assert {row["status"] for row in idle["recipients"]} == {"BACKOFF", "FUNDED"}
    assert w3.eth.get_transaction_count(caller.address) == nonce == 2
    call(receiver.functions.setAvailable(True), "restore receiver availability")
    call(treasury.functions.claimFor(receiver.address), "external caller fulfills need")
    assert all(row["status"] == "FUNDED" for row in worker.tick()["recipients"])
    # Actual funded EOA spends native gas; no SPV publication is represented.
    spend = alice.sign_transaction(dict(to=caller.address, value=1, nonce=0, gas=21000,
                                        gasPrice=w3.eth.gas_price, chainId=31337))
    record(w3.eth.send_raw_transaction(spend.raw_transaction), "payee uses received native gas")

    # A concurrent successful claim can turn our already simulated call into a
    # successful zero payout. Reconcile actual balance, retain its fee/attempt.
    current_balance = w3.eth.get_balance(alice.address)
    second, next_route = route(1, [alice.address], 4 * 10**15, current_balance + 4 * 10**15)
    config["routes"] = [next_route]
    worker = GasCaller(w3, config, identity)
    w3.provider.make_request("evm_increaseTime", [601])
    w3.provider.make_request("evm_mine", [])

    def external_claim_after_simulation(self, step, account, nonce):
        tx = build(self, step, account, nonce)
        call(second.functions.claimFor(alice.address), "external claim after simulation")
        return tx

    monkeypatch.setattr(ClaimRunner, "build_transaction", external_claim_after_simulation)
    raced = worker.tick()
    assert raced["status"] == "CLAIM_NO_PAYOUT" and raced["claimed_wei"] == "0"
    assert raced["recipient"]["status"] == "FUNDED"
    record(raced["transaction_hash"], "successful zero payout reconciled")
    monkeypatch.setattr(ClaimRunner, "build_transaction", build)

    # New canonical proposals for the same epoch do not reset Alice's pair key.
    # Crash after the shared nonce acknowledgement, before budget finalization.
    third, third_route = route(1, [alice.address], 4 * 10**15, w3.eth.get_balance(alice.address) + 4 * 10**15)
    config["routes"] = [third_route]
    worker = GasCaller(w3, config, identity)
    # Advance deterministic local time beyond retry backoff, within the same period.
    w3.provider.make_request("evm_increaseTime", [601])
    w3.provider.make_request("evm_mine", [])
    broadcast = ClaimRunner.broadcast
    monkeypatch.setattr(ClaimRunner, "broadcast", lambda *args: (_ for _ in ()).throw(ConnectionError("offline")))
    waiting = worker.tick()
    assert waiting["status"] == "PENDING"
    assert worker.budget.pending() is not None
    assert w3.eth.get_transaction_count(caller.address) == 3
    saved_hash = waiting["transaction_hash"]
    monkeypatch.setattr(ClaimRunner, "broadcast", broadcast)
    worker = GasCaller(w3, config, identity)
    monkeypatch.setattr(worker.budget, "finish", lambda *args: (_ for _ in ()).throw(RuntimeError("simulated stop")))
    with pytest.raises(RuntimeError, match="simulated stop"):
        worker.tick()
    assert worker.budget.pending()["status"] == "SETTLING"
    nonce = w3.eth.get_transaction_count(caller.address)
    worker = GasCaller(w3, config, identity)
    recovered = worker.tick()
    assert recovered["status"] == "CLAIM_CONFIRMED"
    assert recovered["transaction_hash"] == saved_hash
    assert w3.eth.get_transaction_count(caller.address) == nonce == 4
    record(recovered["transaction_hash"], "recover crash after nonce acknowledgement")
    assert worker.budget.pending() is None
    snapshot = w3.provider.make_request("evm_snapshot", [])["result"]

    # Rotating routes/new proposals cannot replenish the caller's global period count.
    def configure(target, handover):
        terms = [target, sender, 1, Web3.keccak(text="synthetic caller tariff"), 1, handover]
        op = registry.functions.getConfigureOperationHash(terms).call()
        call(auth.functions.fixtureApprove(op, registry.address), "synthetic authority approval")
        call(registry.functions.configure(terms, op), "configure treasury epoch")

    configure(treasury.address, bytes(32))
    bob = Account.from_key((2703).to_bytes(32, "big"))
    _, fourth_route = route(2, [bob.address], 4 * 10**15)
    configure(fourth_route["treasury"], Web3.keccak(text="synthetic explicit handover"))
    config["routes"] = [fourth_route]
    worker = GasCaller(w3, config, identity)
    assert worker.tick()["recipients"][0]["status"] == "PERIOD_ATTEMPTS"
    assert w3.eth.get_transaction_count(caller.address) == 4
    # Route changes preserve three lifetime attempts for Alice's scope/epoch.
    alice_pair = worker.budget.attempts()[1]["request"]["pair"]
    future_period = (w3.eth.get_block("latest").timestamp // 86400 + 1) * 86400
    assert worker.budget.limits(alice_pair, future_period) == "EPOCH_ATTEMPTS"

    # Another process loads the same permanent ledger, with no extra transaction.
    cfg_path = tmp_path / "caller-config.json"
    cfg_path.write_text(json.dumps(config))
    command = [sys.executable, "-m", "signer_agent.gas_caller", "--rpc", rpc,
               "--config", str(cfg_path), "--identity-file", str(identity), "--once"]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=40)
        assert process.returncode == 0, stdout + stderr
        assert json.loads(stdout)["recipients"][0]["status"] == "PERIOD_ATTEMPTS"
    assert w3.eth.get_transaction_count(caller.address) == 4
    # A changed canonical observation stops the caller instead of resetting spent state.
    assert w3.provider.make_request("evm_revert", [snapshot])["result"] is True
    with pytest.raises(ValueError, match="GAS_CALLER_REORG_RECOVERY_REQUIRED"):
        worker.tick()
    report = dict(status="PASS", mode="LOCAL_ANVIL_SYNTHETIC_AUTHORITY_AND_STATE", production_ready=False,
        full_gas_funding_gate="UNKNOWN", zk_proof=False, threshold_signatures=False, spv_publication=False,
        gas_limit=150000, max_fee_per_gas_wei="1000000000", total_caller_transactions=4,
        reorg_simulated_after_receipt_observations=True,
        caller_cost_wei=str(sum(int(row["result"]["cost_wei"]) for row in worker.budget.attempts())),
        availability_failure=failed, funded=paid, concurrent_claim=raced, crash_recovery=recovered,
        receipts=receipts)
    output = os.environ.get("SATOSHIFI_GAS_CALLER_RECEIPT_REPORT")
    if output:
        Path(output).write_text(json.dumps(report, indent=2) + "\n")

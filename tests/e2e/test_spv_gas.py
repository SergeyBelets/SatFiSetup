"""Local receipt evidence: real treasury/router/client, synthetic authority/state.

Run only against a fresh isolated Anvil (31337). No Core, ZK proof, actual DKG,
threshold approval or SPV publication is claimed by this test.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

from eth_account import Account
import pytest
from web3 import Web3

from signer_agent.bootstrap import artifact
from signer_agent.gas_treasury import GasTreasuryClient

ROOT = Path(__file__).resolve().parents[2]


def test_native_gas_receipts_and_durable_cli(tmp_path):
    rpc = os.environ.get("SATOSHIFI_GAS_E2E_RPC")
    fixture_out = os.environ.get("SATOSHIFI_GAS_FIXTURE_OUT")
    if not rpc or not fixture_out:
        pytest.skip("explicit isolated Anvil and private fixture artifacts required")
    w3 = Web3(Web3.HTTPProvider(rpc))
    assert w3.eth.chain_id == 31337
    sender = w3.eth.accounts[0]
    receipts = []

    def record(tx, label, success=True):
        receipt = w3.eth.wait_for_transaction_receipt(tx)
        assert receipt.status == int(success), label
        assert receipt.gasUsed <= 2**24
        assert w3.eth.get_block(receipt.blockNumber).hash == receipt.blockHash
        receipts.append(dict(label=label, transaction_hash=Web3.to_hex(tx),
            block_number=receipt.blockNumber, block_hash=Web3.to_hex(receipt.blockHash),
            status=receipt.status, gas_used=receipt.gasUsed,
            effective_gas_price=receipt.effectiveGasPrice))
        return receipt

    def deploy(name, args=(), fixture=False):
        compiled = (json.loads((Path(fixture_out) / "SPVServicePayment.t.sol" / (name + ".json")).read_text())
                    if fixture else artifact(name))
        factory = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"]["object"])
        receipt = record(factory.constructor(*args).transact({"from": sender, "gas": 2**24}), "deploy:" + name)
        return w3.eth.contract(address=receipt.contractAddress, abi=compiled["abi"]), receipt

    def call(fn, label, **tx):
        return record(fn.transact(dict({"from": sender, "gas": 2**24}, **tx)), label)

    identities = []
    for value in (501, 502, 503):
        account = Account.from_key(value.to_bytes(32, "big"))
        path = tmp_path / (str(value) + ".json")
        path.write_text(json.dumps(dict(private_key=Web3.to_hex(account.key))))
        path.chmod(0o600)
        identities.append((account, path))
    (payer, payer_file), (caller, caller_file), (alice, alice_file) = identities
    record(w3.eth.send_transaction({"from": sender, "to": payer.address, "value": 10**18}), "seed payer")
    record(w3.eth.send_transaction({"from": sender, "to": caller.address, "value": 10**18}), "seed claim caller")
    assert w3.eth.get_balance(alice.address) == 0
    verifier, _ = deploy("GasVerifierFixture", fixture=True)
    coordinator, _ = deploy("GasCoordinatorFixture", [verifier.address], fixture=True)
    auth, _ = deploy("GasAuthorizationFixture", [coordinator.address, [alice.address]], fixture=True)
    factory, _ = deploy("GasConfederationFixture", [auth.address], fixture=True)
    state, _ = deploy("GasStateFixture", fixture=True)
    registry, creation = deploy("SPVServicePaymentRegistry", [factory.address, "conf", state.address])
    price = 3 * 10**15
    treasury_abi = artifact("ConfederationGasTreasury")["abi"]

    def treasury(epoch):
        receipt = call(registry.functions.createTreasury(epoch, [alice.address, caller.address], [1, 2]), "create treasury")
        address = registry.events.TreasuryCreated().process_receipt(receipt)[0]["args"]["treasury"]
        return w3.eth.contract(address=address, abi=treasury_abi)

    def configure(target, handover):
        config = [target.address, sender, 1, Web3.keccak(text="synthetic tariff"), price, handover]
        op = registry.functions.getConfigureOperationHash(config).call()
        call(auth.functions.fixtureApprove(op, registry.address), "synthetic configuration approval")
        call(registry.functions.configure(config, op), "configure")

    first = treasury(1)
    configure(first, b"\0" * 32)
    binding = dict(schema="satoshifi.spv-gas-binding.v1", chain_id="31337", evm_confirmations="1",
        registry=registry.address, registry_creation_block=str(creation.blockNumber),
        registry_runtime_hash=Web3.to_hex(Web3.keccak(w3.eth.get_code(registry.address))),
        factory=factory.address, confederation_id="conf", state_view=state.address,
        authorization=auth.address, domain=Web3.to_hex(registry.functions.domain().call()))
    binding_file = tmp_path / "binding.json"
    binding_file.write_text(json.dumps(binding))

    def cli(*args, success=True):
        process = subprocess.run([sys.executable, "-m", "signer_agent.gas_treasury", "--rpc", rpc,
            "--binding", str(binding_file), *map(str, args)], capture_output=True, text=True,
            env=dict(os.environ, PYTHONPATH=str(ROOT / "signer-agent")), timeout=40)
        assert process.returncode == (0 if success else 1), process.stdout + process.stderr
        return json.loads(process.stdout)

    def admit(job, previous=0):
        terms = [job, Web3.keccak(b"semantic" + job), Web3.keccak(text="fed"), payer.address,
                 Web3.keccak(text="local budget"), deadline]
        call(registry.functions.admit(terms, state.functions.digest().call(), state.functions.version().call(),
            deadline - 1, previous), "admit")
        return registry.functions.permissionNonce().call()

    def submit(request, identity, name):
        path = tmp_path / (name + ".json"); path.write_text(json.dumps(request))
        result = cli("submit", "--request", path, "--identity-file", identity)
        assert result["status"] == "CONFIRMED"
        record(Web3.to_bytes(hexstr=result["transaction_hash"]), name)
        return result, path

    deadline = w3.eth.get_block("latest").timestamp + 3600
    job = Web3.keccak(text="receipt job")
    nonce = admit(job)
    request = cli("prepare-fund", "--job", Web3.to_hex(job))
    fund, fund_path = submit(request, payer_file, "fund")
    payer_nonce = w3.eth.get_transaction_count(payer.address)
    assert cli("submit", "--request", fund_path, "--identity-file", payer_file) == fund
    assert w3.eth.get_transaction_count(payer.address) == payer_nonce
    assert registry.functions.paid(job).call()
    assert w3.eth.get_balance(first.address) == price
    request = cli("prepare-claim", "--treasury", first.address, "--payee", alice.address)
    claim, claim_path = submit(request, caller_file, "claim zero-balance payee")
    assert w3.eth.get_balance(alice.address) == price // 3
    caller_nonce = w3.eth.get_transaction_count(caller.address)
    assert cli("submit", "--request", claim_path, "--identity-file", caller_file) == claim
    assert w3.eth.get_transaction_count(caller.address) == caller_nonce
    # A separate signed transaction proves the zero-balance recipient now has
    # usable gas. This is a native transfer, NOT an SPV publication/message.
    tx = dict(to=caller.address, value=1, nonce=0, gas=21000, gasPrice=w3.eth.gas_price, chainId=31337)
    signed = alice.sign_transaction(tx)
    record(w3.eth.send_raw_transaction(signed.raw_transaction), "payee spends native gas")

    # Changed state rejects value atomically. Explicit revision retains job ID,
    # payer/deadline and lets the same unpaid job use a new permission nonce.
    second_job = Web3.keccak(text="state-race job")
    old_nonce = admit(second_job)
    prepared = cli("prepare-fund", "--job", Web3.to_hex(second_job))
    call(state.functions.change(Web3.keccak(text="next synthetic state")), "change synthetic state")
    target_before = w3.eth.get_balance(first.address)
    payer_before = w3.eth.get_balance(payer.address)
    data = registry.functions.fundRequest(second_job, old_nonce)._encode_transaction_data()
    tx = dict(to=registry.address, value=price, data=data, nonce=payer_nonce, gas=500000,
              gasPrice=w3.eth.gas_price, chainId=31337)
    failed = record(w3.eth.send_raw_transaction(payer.sign_transaction(tx).raw_transaction),
                    "stale state returns value", success=False)
    assert not registry.functions.paid(second_job).call()
    assert w3.eth.get_balance(first.address) == target_before
    assert w3.eth.get_balance(payer.address) == payer_before - failed.gasUsed * failed.effectiveGasPrice
    admit(second_job, old_nonce)
    stale = tmp_path / "stale.json"; stale.write_text(json.dumps(prepared))
    rejected = cli("submit", "--request", stale, "--identity-file", payer_file, success=False)
    assert rejected == dict(status="BLOCKED", reason="GAS_REQUEST_CHANGED")
    assert w3.eth.get_transaction_count(payer.address) == payer_nonce + 1
    replacement = cli("prepare-fund", "--job", Web3.to_hex(second_job))
    submit(replacement, payer_file, "fund revised job")

    next_treasury = treasury(2)
    configure(next_treasury, Web3.keccak(text="synthetic handover plan"))
    assert registry.functions.payments(job).call()[0] == first.address
    assert registry.functions.paid(job).call()
    assert cli("prepare-fund", "--job", Web3.to_hex(job), success=False)["status"] == "BLOCKED"
    old_claim = cli("prepare-claim", "--treasury", first.address, "--payee", caller.address)
    submit(old_claim, caller_file, "old epoch claim")
    assert w3.eth.get_balance(next_treasury.address) == 0
    # Full queue bound with actual separate capped transactions.
    for number in range(1, 257):
        admit(Web3.keccak(text="queue job " + str(number)))
    assert registry.functions.pendingCount().call() == 256
    with pytest.raises(Exception, match="pending limit"):
        terms = [Web3.keccak(text="overflow"), Web3.keccak(text="overflow semantic"), Web3.keccak(text="fed"),
                 payer.address, Web3.keccak(text="local budget"), deadline]
        registry.functions.admit(terms, state.functions.digest().call(), state.functions.version().call(),
                                deadline - 1, 0).call({"from": sender})
    report = dict(status="PASS", mode="LOCAL_ANVIL_SYNTHETIC_AUTHORITY_AND_STATE", production_ready=False,
        scope="Real contracts/client/receipts; authority approvals and SPV state are synthetic fixtures",
        full_gas_funding_gate="UNKNOWN", zk_proof=False, threshold_signatures=False, spv_publication=False,
        zero_balance_claim_and_native_transaction=True, duplicate_cli_preserves_nonce=True,
        queue_admissions=256, transaction_gas_limit=2**24, binding=binding,
        treasuries=[first.address, next_treasury.address], price_wei=str(price), receipts=receipts)
    output = os.environ.get("SATOSHIFI_GAS_RECEIPT_REPORT")
    if output:
        Path(output).write_text(json.dumps(report, indent=2) + "\n")

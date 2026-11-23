"""ZK state contract deployed before enrollment and adopted by bootstrap.

The state contract (ZkBitcoinStateV2 with its adapter and verifier) depends
only on the enrolled Bitcoin policy; its confederation ID comes from the
launch identifiers. Deploying it before participants enroll lets workers
catch up from the checkpoint while the roster is still being collected.

The invitation commits {address, tx_hash}. Bootstrap then verifies, instead of
deploying, that this exact creation transaction built the relay step of the
signed plan: same artifact build, same constructor input, finalized, and the
same post-deploy checks. Any mismatch stops bootstrap.
"""
import argparse
import json
import re
import sys
import time

from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound

ZK_STATE_ARTIFACTS = ("ZkBitcoinStateV2", "ZkBitcoinTestnet4StateV2")
TX_HASH = re.compile(r"0x[0-9a-f]{64}")


def validate_adoption(value, policy):
    from .zk_spv import is_zk
    if (not isinstance(value, dict) or set(value) != {"address", "tx_hash"}
            or not isinstance(value["address"], str) or not Web3.is_checksum_address(value["address"])
            or int(value["address"], 16) == 0
            or not isinstance(value["tx_hash"], str) or not TX_HASH.fullmatch(value["tx_hash"])):
        raise ValueError("ZK_RELAY_ADOPTION_SCHEMA")
    if not is_zk(policy):
        raise ValueError("ZK_RELAY_REQUIRES_ZK_POLICY")
    return value


def relay_step(policy):
    """The relay step exactly as bootstrap_plan builds it (deploy, outputs, checks)."""
    from .bitcoin_network import validate_policy
    from .bootstrap import Bootstrap
    from .zk_profiles import profile as zk_profile
    from .zk_spv import is_zk, constructor_args, checkpoint_descriptor
    policy = validate_policy(policy, policy.get("network"))
    if not is_zk(policy):
        raise ValueError("ZK_RELAY_REQUIRES_ZK_POLICY")
    selected = zk_profile(policy)
    args = constructor_args(policy)
    checks = [
        dict(contract="relay", method=method, args=[], equals=Bootstrap.wire(value))
        for method, value in (
            ("confederationId", args[0][0]), ("programImageId", args[0][1]),
            ("RELAY_PROFILE", args[0][2]), ("maxStateAge", args[0][3]),
            ("maxTipLag", args[0][4]), ("baselineMaxTipAge", args[0][5]),
            ("checkpointDescriptor", checkpoint_descriptor(policy)),
        )
    ]
    return dict(artifact=selected["state"], args=list(Bootstrap.wire(args)), adapter=selected["adapter"],
                checks=checks)


def _compiled(name):
    from .bootstrap import artifact
    from .zk_spv import require_patched_build
    compiled = artifact(name)
    if name in ZK_STATE_ARTIFACTS:
        require_patched_build(compiled)
    return compiled


def _creation(w3, compiled, args):
    from .bootstrap import encode_args
    constructor = next(entry for entry in compiled["abi"] if entry["type"] == "constructor")
    return w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"]["object"]).constructor(
        *encode_args(constructor["inputs"], args))


def _call(w3, address, abi, method, block):
    return getattr(w3.eth.contract(address=address, abi=abi).functions, method)().call(block_identifier=block)


def verify_deployment(w3, policy, address, tx_hash, *, block=None):
    """Read-only check of a creation transaction against the relay step."""
    from .bootstrap import Bootstrap, artifact
    from .bitcoin_trust import _check_runtime
    from .contract_provenance import _creation_input
    step = relay_step(policy)
    compiled = _compiled(step["artifact"])
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    if receipt.status != 1 or receipt.contractAddress != address:
        raise ValueError("ZK_RELAY_ADOPTION_MISMATCH")
    transaction = w3.eth.get_transaction(tx_hash)
    constructor = next(entry for entry in compiled["abi"] if entry["type"] == "constructor")
    from .bootstrap import encode_args
    expected_input = _creation_input(compiled, encode_args(constructor["inputs"], step["args"]))
    code = w3.eth.get_code(address, block_identifier=receipt.blockNumber)
    _check_runtime(code, compiled["deployedBytecode"])
    if transaction["to"] is not None or HexBytes(transaction["input"]) != expected_input:
        raise ValueError("ZK_RELAY_ADOPTION_MISMATCH")
    at = receipt.blockNumber if block is None else block
    for check in step["checks"]:
        if Bootstrap.wire(_call(w3, address, compiled["abi"], check["method"], at)) != check["equals"]:
            raise ValueError("ZK_RELAY_CHECK_FAILED:" + check["method"])
    adapter = _call(w3, address, compiled["abi"], "adapter", at)
    verifier = _call(w3, address, compiled["abi"], "verifier", at)
    adapter_abi = artifact(step["adapter"])["abi"]
    if _call(w3, adapter, adapter_abi, "state", at) != address or _call(w3, adapter, adapter_abi, "testHelpersEnabled", at):
        raise ValueError("ZK_RELAY_ADAPTER_MISMATCH")
    return dict(address=address, tx_hash=tx_hash, block_number=receipt.blockNumber, adapter=adapter,
                verifier=verifier, artifact=step["artifact"])


def adopt_step(runner, step, key):
    """Bootstrap: confirm an enrolled pre-deployed relay instead of deploying it."""
    from .bootstrap import artifact_fingerprint
    adopt = step["adopt"]
    try:
        receipt = runner.w3.eth.get_transaction_receipt(adopt["tx_hash"])
    except TransactionNotFound:
        raise ValueError("ZK_RELAY_ADOPTION_NOT_FOUND") from None
    if receipt.status != 1 or receipt.contractAddress != adopt["address"]:
        raise ValueError("ZK_RELAY_ADOPTION_MISMATCH")
    result = dict(tx_hash=adopt["tx_hash"], block_number=str(receipt.blockNumber), block_hash=receipt.blockHash.hex())
    if runner.verify_receipt(result, receipt=receipt) is None:
        runner.journal.update(key, "CONFIRMING", result)
        return dict(status="CONFIRMING", step=step["id"], tx_hash=result["tx_hash"])
    compiled = _compiled(step["artifact"])
    code = runner.w3.eth.get_code(receipt.contractAddress, block_identifier=receipt.blockNumber)
    try:
        runner.verify_previous_creation(step, result, receipt, compiled, code)
    except ValueError as error:
        raise ValueError("ZK_RELAY_ADOPTION_MISMATCH") from error
    result["contract"] = dict(address=receipt.contractAddress, artifact=step["artifact"],
                              artifact_hash=artifact_fingerprint(compiled), runtime_hash=Web3.keccak(code).hex())
    runner.outputs(step, result)
    runner.verify_checks(step, receipt)
    runner.journal.update(key, "CONFIRMED", result)
    return None


def deploy(w3, policy, account, *, dry_run=False, wait_seconds=900):
    from .bootstrap_create import create_address
    from .deployment_limits import check_deployment
    from .evm_network import transaction_policy, transaction_gas_with_margin
    step = relay_step(policy)
    compiled = _compiled(step["artifact"])
    function = _creation(w3, compiled, step["args"])
    check_deployment(compiled["deployedBytecode"]["object"], function.data_in_transaction)
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    transaction = dict(chainId=w3.eth.chain_id, nonce=nonce, value=0, **transaction_policy(w3))
    transaction["from"] = account.address
    transaction["gas"] = function.estimate_gas(transaction, block_identifier="pending")
    transaction = transaction_gas_with_margin(w3, function.build_transaction(transaction))
    predicted = create_address(account.address, nonce)
    plan = dict(status="DRY_RUN" if dry_run else "SENT", artifact=step["artifact"], deployer=account.address,
                nonce=nonce, predicted_address=predicted, gas=transaction["gas"],
                max_fee_per_gas=transaction.get("maxFeePerGas"), chain_id=transaction["chainId"])
    if dry_run:
        return plan
    signed = account.sign_transaction(transaction)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    tx_hash = tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            break
        except TransactionNotFound:
            if time.monotonic() > deadline:
                return dict(plan, tx_hash=tx_hash, status="PENDING")
            time.sleep(6)
    if receipt.status != 1 or receipt.contractAddress != predicted:
        raise ValueError("ZK_RELAY_DEPLOY_FAILED")
    return dict(plan, status="DEPLOYED", address=receipt.contractAddress, tx_hash=tx_hash,
                block_number=receipt.blockNumber)


def main(argv=None):
    from .evm_rpc import evm_web3
    from .onboarding import read_private, read_public
    from eth_account import Account
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("action", choices=("deploy", "verify"))
    p.add_argument("--bitcoin-policy", required=True, help="Enrolled ZK Bitcoin policy JSON (version 2)")
    p.add_argument("--rpc", required=True)
    p.add_argument("--chain-id", required=True, choices=("1", "11155111", "31337"))
    p.add_argument("--identity-file", help="deploy: local EVM identity that pays for the creation")
    p.add_argument("--adoption", help="verify: JSON {address, tx_hash} written by deploy")
    p.add_argument("--output", help="deploy: write {address, tx_hash} for setup invite --zk-relay")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    try:
        w3 = evm_web3(a.rpc, timeout=30)
        if str(w3.eth.chain_id) != a.chain_id:
            raise ValueError("CHAIN_MISMATCH")
        policy = read_public(a.bitcoin_policy)
        if a.action == "deploy":
            if not a.identity_file or (not a.dry_run and not a.output):
                p.error("deploy requires --identity-file and, unless --dry-run, --output")
            account = Account.from_key(json.loads(read_private(a.identity_file))["private_key"])
            result = deploy(w3, policy, account, dry_run=a.dry_run)
            if result.get("status") == "DEPLOYED":
                from pathlib import Path
                out = Path(a.output)
                if out.exists():
                    raise ValueError("OUTPUT_EXISTS")
                out.write_text(json.dumps(dict(address=result["address"], tx_hash=result["tx_hash"]), indent=2) + "\n")
        else:
            if not a.adoption:
                p.error("verify requires --adoption")
            adoption = validate_adoption(read_public(a.adoption), policy)
            result = dict(status="VERIFIED", **verify_deployment(w3, policy, adoption["address"], adoption["tx_hash"]))
        print(json.dumps(result, indent=2, default=str))
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps(dict(status="ERROR", code=str(error) or type(error).__name__, action=a.action)))
        return 2


if __name__ == "__main__":
    sys.exit(main())

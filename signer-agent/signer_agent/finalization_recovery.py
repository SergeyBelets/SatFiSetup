"""Reconcile a losing finalizeSession transaction without changing its receipt."""

from hexbytes import HexBytes
from web3 import Web3

from .bootstrap import Bootstrap, artifact
from .bootstrap_recovery import anchor, signed_fields
from .bitcoin_trust import checked_runtime
from .operations import canonical
from .transaction_wait import TransactionPending


def is_finalization_step(saved, entry):
    step = entry["request"]
    return (saved["operation"].startswith("signer-") and step.get("kind") == "call"
            and step.get("method") == "finalizeSession" and step.get("id") == "send")


def reconcile(w3, lane, account, saved, source, step, receipt):
    """Consume the local nonce only if the exact result already won on-chain."""
    fields = signed_fields(saved, account, w3.eth.chain_id)
    if not fields["to"] or fields["value"] != 0:
        return False
    target = Web3.to_checksum_address(fields["to"])
    coordinator = w3.eth.contract(address=target, abi=artifact("FROSTCoordinator")["abi"])
    fn, args = coordinator.decode_function_input(fields["data"])
    if fn.fn_name != "finalizeSession":
        return False
    values = Bootstrap.wire([args["sessionId"], args["aggregatedSignature"], args["messageHash"]])
    operation = "signer-" + Web3.keccak(text=canonical([target, fn.fn_name, values])).hex()
    if saved["operation"] != operation + ":send" or values != step.get("args"):
        return False

    profile = source.get(operation)["request"]["profile"]
    spec = profile.get("contracts", {}).get(step.get("contract"), {})
    if (profile.get("steps") != [step] or spec.get("artifact") != "FROSTCoordinator"
            or spec.get("address") != target or profile.get("chain_id") != saved["chain_id"]):
        return False

    at = receipt.blockNumber
    checked_runtime(w3, target, "FROSTCoordinator", at)
    session = coordinator.functions.isFinalized(args["sessionId"])
    if not session.call(block_identifier=at):
        return False
    result = coordinator.functions.getResult(args["sessionId"]).call(block_identifier=at)
    if (bytes(result[0]), bytes(result[1])) != (args["aggregatedSignature"], args["messageHash"]):
        return False

    block = anchor(w3)
    if receipt.blockNumber > block.number:
        raise TransactionPending(dict(step=step["id"], tx_hash=saved["tx_hash"],
                                      reason="FROST_FINALIZATION_REVERT_AWAITING_FINALITY"))
    sender = account.address if account is not None else w3.eth.account.recover_transaction(HexBytes(saved["raw_transaction"]))
    if (w3.eth.get_transaction_count(sender, block.number) <= fields["nonce"]
            or w3.eth.get_block(receipt.blockNumber).hash != receipt.blockHash
            or w3.eth.get_block(block.number).hash != block.hash):
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")

    lane.update(saved["tx_hash"], "CONFIRMED", dict(tx_hash=saved["tx_hash"], nonce=str(fields["nonce"]),
        receipt_status="0", block_number=str(receipt.blockNumber), block_hash=receipt.blockHash.hex()))
    return True

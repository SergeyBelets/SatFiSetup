"""Recognize one already-satisfied, reverted FROST finalization in its EOA journal."""
import fcntl
import hashlib
import json
import os
from pathlib import Path

from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound

from . import bootstrap_recovery
from .evm_network import read_block
from .operations import Journal, canonical
from .transaction_wait import TransactionPending, TransactionRecoveryRequired


PACKAGED_ABI = Path(__file__).resolve().parents[1] / "contracts/FROSTCoordinator.json"
FUNCTION_TYPES = {
    "finalizeSession": (["uint256", "bytes", "bytes32"], []),
    "getResult": (["uint256"], ["bytes", "bytes32"]),
    "getSession": (["uint256"], [
        "uint256", "address", "bytes", "bytes32", "bool", "uint256", "uint256", "uint64", "bool",
        "address", "uint256", "uint256", "uint256", "uint256", "uint256", "address", "uint256",
        "uint16", "bytes32", "uint256"]),
}


def _usable_abi(abi):
    if not isinstance(abi, list) or not all(isinstance(item, dict) for item in abi):
        return False
    for name, types in FUNCTION_TYPES.items():
        entries = [item for item in abi if item.get("name") == name and item.get("type") == "function"]
        if len(entries) != 1 or tuple([arg["type"] for arg in entries[0][key]] for key in ("inputs", "outputs")) != types:
            return False
    events = [item for item in abi if item.get("name") == "SessionFinalized" and item.get("type") == "event"]
    return (len(events) == 1 and events[0].get("anonymous") is False and
            [(a["name"], a["type"], a["indexed"]) for a in events[0]["inputs"]] ==
            [("sessionId", "uint256", True), ("aggregatedSignature", "bytes", False), ("messageHash", "bytes32", False)])


def _recovery_contract(w3, address, client_contract=None):
    if client_contract is not None and client_contract.address == address:
        try:
            if _usable_abi(getattr(client_contract, "abi", None)):
                return client_contract
        except (KeyError, TypeError):
            pass
    for filename in (PACKAGED_ABI, os.environ.get("FROST_ABI_PATH")):
        if filename:
            try:
                abi = json.loads(Path(filename).expanduser().read_text(encoding="utf-8"))
                if isinstance(abi, dict):
                    abi = abi.get("abi")
                if _usable_abi(abi):
                    return w3.eth.contract(address=address, abi=abi)
            except (OSError, ValueError, TypeError, KeyError):
                continue
    return None


def _operation(address, args):
    return "signer-" + Web3.keccak(text=canonical([address, "finalizeSession", args])).hex()


def _result_matches(contract, session_id, signature, message, number):
    result = contract.functions.getResult(session_id).call(block_identifier=number)
    return bytes(result[0]) == signature and bytes(result[1]) == message


def _winner_preceded(contract, receipt, session_id, signature, message):
    number = receipt.blockNumber
    logs = contract.events.SessionFinalized().get_logs(
        from_block=number, to_block=number, argument_filters={"sessionId": session_id})
    if logs:
        return any(
            log["address"] == contract.address and log["blockHash"] == receipt.blockHash
            and not log.get("removed", False) and log["transactionIndex"] < receipt.transactionIndex
            and log["args"]["sessionId"] == session_id
            and bytes(log["args"]["aggregatedSignature"]) == signature
            and bytes(log["args"]["messageHash"]) == message for log in logs)
    if number == 0:
        return False
    previous = contract.functions.getSession(session_id).call(block_identifier=number - 1)
    return previous[10] == 2 and _result_matches(contract, session_id, signature, message, number - 1)


def settle_revert(w3, lane, source, saved, entry, account, confirmations, receipt=None, client_contract=None):
    """Caller holds the EOA lock. Leave every other revert in ordinary recovery."""
    step = entry["request"]
    if (account is None or step.get("method") != "finalizeSession"
            or entry["status"] not in ("PREPARED", "SIGNED", "BROADCAST", "CONFIRMING", "REVERTED")):
        return False
    source_path = str(source.path.resolve())
    if not source_path.endswith(".transactions.sqlite"):
        return False
    identity = source_path.removesuffix(".transactions.sqlite")
    if lane.path.resolve() != Path(str(bootstrap_recovery.nonce_lock_path(identity)) + ".sqlite"):
        return False
    args = step.get("args", [])
    if (step != dict(id="send", kind="call", contract="target", method="finalizeSession", args=args)
            or len(args) != 3 or not saved["operation"].startswith("signer-")
            or not saved["operation"].endswith(":send")):
        return False
    prefix = saved["operation"][:-5]
    profile = source.get(prefix)["request"]["profile"]
    target = profile["contracts"]["target"]
    if (target.get("artifact") != "FROSTCoordinator" or profile["steps"] != [step]
            or _operation(target["address"], args) != prefix or profile["deployment_id"] != prefix):
        return False
    if (profile["chain_id"] != str(w3.eth.chain_id) or saved["chain_id"] != profile["chain_id"]
            or saved["source_journal"] != str(source.path.resolve())
            or saved["step_hash"] != hashlib.sha256(canonical(step).encode()).hexdigest()
            or lane.get(saved["tx_hash"])["request"] != saved):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    fields = bootstrap_recovery.signed_fields(saved, account, w3.eth.chain_id)
    result = entry["result"]
    envelope = {k: saved[k] for k in ("tx_hash", "nonce", "raw_transaction")}
    if any(result[k] != value for k, value in envelope.items() if k in result):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    if receipt is None:
        try:
            receipt = w3.eth.get_transaction_receipt(saved["tx_hash"])
        except TransactionNotFound:
            return False
    bootstrap_recovery.check_receipt_binding(receipt, dict(result, **envelope))
    if receipt.status != 0 or receipt.gasUsed >= fields["gas"]:
        return False
    session_id, signature, message = int(args[0]), HexBytes(args[1]), HexBytes(args[2])
    contract = _recovery_contract(w3, target["address"], client_contract)
    if contract is None:
        return False
    call_data = contract.encode_abi("finalizeSession", args=[session_id, signature, message])
    if (len(signature) != 64 or len(message) != 32 or fields["value"] != 0
            or bytes(fields["to"]) != HexBytes(contract.address) or fields["data"] != HexBytes(call_data)):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    if w3.eth.get_block(receipt.blockNumber).hash != receipt.blockHash:
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    if not _winner_preceded(contract, receipt, session_id, signature, message):
        return False
    block = read_block(w3, max(confirmations, int(profile.get("evm_confirmations", "1"))))
    # Protected EOA floors also require consensus finality on Sepolia.
    if w3.eth.chain_id not in (1, 31337):
        final = bootstrap_recovery.anchor(w3)
        if final.number < block.number:
            block = final
    if receipt.blockNumber > block.number:
        raise TransactionPending(dict(step="send", tx_hash=saved["tx_hash"], reason="FINALIZATION_AWAITING_FINALITY"))
    session = contract.functions.getSession(session_id).call(block_identifier=block.number)
    if (session[0] != session_id or session[10] != 2 or bytes(session[3]) != message
            or not session[4] or not session[8]
            or not _result_matches(contract, session_id, signature, message, block.number)
            or Web3.keccak(w3.eth.get_code(contract.address, block.number)) != HexBytes(target["runtime_hash"])
            or w3.eth.get_transaction_count(account.address, block.number) <= fields["nonce"]):
        return False
    if (w3.eth.get_block(block.number).hash != block.hash
            or w3.eth.get_block(receipt.blockNumber).hash != receipt.blockHash):
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    # Source first. A crash before updating the lane repeats all checks.
    result = dict(result, **envelope)
    result.update(block_number=str(receipt.blockNumber), block_hash=receipt.blockHash.hex())
    if entry["status"] != "REVERTED" or result != entry["result"]:
        source.update(saved["operation"], "REVERTED", result)
    lane.update(saved["tx_hash"], "CONFIRMED", dict(
        tx_hash=saved["tx_hash"], nonce=str(fields["nonce"]), receipt_status="0",
        block_number=result["block_number"], block_hash=result["block_hash"],
        reason="SESSION_ALREADY_FINALIZED", session=str(session_id), operation_hash=message.hex()))
    return True


def reconcile_session(client, session_id, signature, message, *, terminal=False):
    """Resume the exact attempted finalization, including after a process restart."""
    identity = getattr(client, "identity_file", None)
    journal = getattr(client, "transaction_journal", None)
    if not identity or not journal or not Path(journal).is_file():
        return False
    from .bootstrap import Bootstrap
    args = Bootstrap.wire([session_id, signature, message])
    key = _operation(client.address, args) + ":send"
    source = Journal(client.transaction_journal)
    try:
        entry = source.get(key)
    except KeyError:
        return False
    tx_hash = entry["result"].get("tx_hash")
    if not tx_hash and not Path(str(bootstrap_recovery.nonce_lock_path(identity)) + ".sqlite").is_file():
        return False
    account, lock, lane = bootstrap_recovery.identity_lane(identity, require_existing=True)
    if account.address != client.sender:
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if not tx_hash:
            with lane.db() as db:
                rows = db.execute(
                    "SELECT id FROM operations WHERE json_extract(request, '$.source_journal')=? "
                    "AND json_extract(request, '$.operation')=?", (str(source.path.resolve()), key)
                ).fetchall()
            if not rows:
                return False
            if len(rows) != 1:
                raise ValueError("EVM_NONCE_CONFLICT_RECOVERY_REQUIRED")
            tx_hash = rows[0]["id"]
        row = lane.get(tx_hash)
        if row["request"].get("tx_hash") != tx_hash or row["request"].get("operation") != key:
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        if row["status"] == "RETIRED":
            return False
        bootstrap_recovery.protected_nonce_floor(client.w3, lane, account, client.chain_id)
        if row["status"] == "CONFIRMED":
            return (row["request"].get("operation") == key
                    and row["result"].get("reason") == "SESSION_ALREADY_FINALIZED"
                    and row["result"].get("receipt_status") == "0")
        entry = source.get(key)
        resolved = settle_revert(client.w3, lane, source, row["request"], entry, account,
                                 int(client._confirmation_graph().get("evm_confirmations", "1")),
                                 client_contract=getattr(client, "contract", None))
        if terminal and not resolved:
            # Contract finalization by itself does not release a failed EOA reservation.
            # A manual acknowledgement or a successful transaction was handled above.
            reverted = entry["status"] == "REVERTED"
            if not reverted and entry["status"] in ("PREPARED", "SIGNED", "BROADCAST", "CONFIRMING"):
                try:
                    receipt = client.w3.eth.get_transaction_receipt(tx_hash)
                except TransactionNotFound:
                    return False
                bootstrap_recovery.check_receipt_binding(receipt, dict(entry["result"], tx_hash=tx_hash))
                if receipt.status == 0:
                    if client.w3.eth.get_block(receipt.blockNumber).hash != receipt.blockHash:
                        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
                    reverted = True
            if reverted:
                raise TransactionRecoveryRequired(dict(step="send", tx_hash=tx_hash,
                    reason="FINALIZATION_REVERT_REQUIRES_REVIEW"))
        return resolved


def reconcile_terminal(client, session):
    """Look only for a local finalize call for this confirmed session before any RPC."""
    journal = getattr(client, "transaction_journal", None)
    if not getattr(client, "identity_file", None) or not journal or not Path(journal).is_file():
        return
    source = Journal(journal)
    with source.db() as db:
        rows = db.execute(
            "SELECT id,request FROM operations WHERE status IN ('PREPARED','SIGNED','BROADCAST','CONFIRMING','REVERTED') "
            "AND json_extract(request, '$.method')='finalizeSession' AND json_extract(request, '$.args[0]')=? "
            "AND json_extract(request, '$.args[2]')=?", (str(session.session_id), "0x" + bytes(session.message_hash).hex())
        ).fetchall()
    for row in rows:
        args = json.loads(row["request"])["args"]
        if row["id"] == _operation(client.address, args) + ":send":
            reconcile_session(client, session.session_id, HexBytes(args[1]), session.message_hash, terminal=True)

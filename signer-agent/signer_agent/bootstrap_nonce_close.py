"""Operator-approved nonce consumption for a failed withdrawal CREATE group."""

import hashlib

from hexbytes import HexBytes
from web3.exceptions import TransactionNotFound

from .evm_network import _limit, check_transaction_gas
from .operations import canonical
from .safe_errors import emit_debug

CLOSE_KIND = "withdrawal-nonce-close"
GAS = 21_000


def close_key(attempt):
    return CLOSE_KIND + ":" + attempt["tx_hash"]


def binding(bootstrap, account, attempt, group):
    chain_id = bootstrap.w3.eth.chain_id
    if int(bootstrap.profile.get("chain_id", chain_id)) != chain_id:
        raise ValueError("CHAIN_MISMATCH")
    return dict(kind=CLOSE_KIND, chain_id=str(chain_id), operator=account.address,
                deployment=bootstrap.prefix, recovery_source=str(bootstrap.journal.path.resolve()),
                operation=attempt["operation"], original_tx_hash=attempt["tx_hash"], nonce=attempt["nonce"],
                create_hashes=[item["tx_hash"] for item in group])


def fingerprint(proposal):
    return hashlib.sha256(canonical(proposal).encode()).hexdigest()


def reserved_close(lane, key, expected, account):
    try:
        saved = lane.get(key)["request"]
    except KeyError:
        return None
    validate_close(saved, expected, account)
    return saved


def validate_close(saved, expected, account):
    from .bootstrap_recovery import signed_fields

    if saved.get("proposal") is None or any(saved.get(k) != v for k, v in expected.items()):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    fields = signed_fields(saved, account, int(expected["chain_id"]))
    proposal = saved["proposal"]
    if (fields.get("type") != 2 or fields["to"] != HexBytes(account.address) or fields["value"]
            or fields["data"] or fields["gas"] != GAS or fields["accessList"]
            or proposal != dict(expected, to=account.address, gas=str(GAS), value="0", data="0x",
                                max_fee_per_gas_wei=str(fields["maxFeePerGas"]),
                                max_priority_fee_per_gas_wei=str(fields["maxPriorityFeePerGas"]))):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")


def observed_tx(w3, tx_hash):
    try:
        tx = w3.eth.get_transaction(tx_hash)
    except TransactionNotFound:
        return None
    if tx["hash"] != HexBytes(tx_hash):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    return tx


def minimum_fees(original):
    old_cap = original.get("maxFeePerGas", original.get("gasPrice"))
    old_priority = original.get("maxPriorityFeePerGas", old_cap)
    if old_cap is None:
        raise ValueError("CREATE_GROUP_NONCE_CLOSE_FEE")
    return (max(old_cap + 1, (old_cap * 110 + 99) // 100),
            max(old_priority + 1, (old_priority * 110 + 99) // 100))


def close_proposal(expected, minimum):
    max_fee = _limit("SATOSHIFI_MAX_FEE_PER_GAS_WEI")
    priority = _limit("SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI")
    if (not 0 < priority <= max_fee <= (2**256 - 1) // GAS
            or max_fee < minimum[0] or priority < minimum[1]):
        raise ValueError("CREATE_GROUP_NONCE_CLOSE_FEE")
    return dict(expected, to=expected["operator"], gas=str(GAS), value="0", data="0x",
                max_fee_per_gas_wei=str(max_fee), max_priority_fee_per_gas_wei=str(priority))


def public_review(proposal):
    fields = {key: value for key, value in proposal.items() if key != "recovery_source"}
    fields["max_cost_wei"] = str(GAS * int(proposal["max_fee_per_gas_wei"]))
    return fields


def close_nonce(bootstrap, lane, account, block, next_nonce, attempt, original, *, group, prepare, confirmed):
    w3 = bootstrap.w3
    key = close_key(attempt)
    expected = binding(bootstrap, account, attempt, group)
    saved = reserved_close(lane, key, expected, account)
    minimum = minimum_fees(original)
    if saved is None and not prepare and confirmed is None:
        return dict(status="CREATE_GROUP_NONCES_NOT_FINALIZED", minimum_max_fee_per_gas_wei=str(minimum[0]),
                    minimum_max_priority_fee_per_gas_wei=str(minimum[1]))
    proposal = saved["proposal"] if saved else close_proposal(expected, minimum)
    proof = fingerprint(proposal)
    if confirmed is not None and confirmed != proof:
        raise ValueError("CREATE_GROUP_NONCE_CLOSE_FINGERPRINT")

    nonce = int(attempt["nonce"])
    latest = w3.eth.get_block("latest")
    latest_nonce = w3.eth.get_transaction_count(account.address, latest.number)
    if nonce != next_nonce or latest.number < block.number or latest_nonce < next_nonce:
        raise ValueError("CREATE_GROUP_NONCE_CLOSE_CONFLICT")
    result = dict(nonce_close=public_review(proposal), fingerprint=proof)
    if saved:
        result["tx_hash"] = saved["tx_hash"]
    if latest_nonce > nonce:
        return dict(status="CREATE_GROUP_NONCE_CLOSE_CONFIRMING", **result)
    if saved and observed_tx(w3, saved["tx_hash"]) is not None:
        return dict(status="CREATE_GROUP_NONCE_CLOSE_BROADCAST", **result)
    known = observed_tx(w3, attempt["tx_hash"])
    if known is not None and known["blockNumber"] is not None:
        return dict(status="CREATE_GROUP_NONCE_CLOSE_CONFIRMING", **result)
    if any(observed_tx(w3, item["tx_hash"]) is not None for item in group
           if item["receipt_status"] == "UNCONSUMED" and int(item["nonce"]) > nonce):
        raise ValueError("CREATE_GROUP_NONCE_CLOSE_CONFLICT")
    deadline = bootstrap.profile.get("approval_deadline")
    if saved is None and deadline is not None and latest.timestamp >= int(deadline):
        raise ValueError("CREATE_GROUP_RECOVERY_APPROVAL_EXPIRED")
    pending = w3.eth.get_transaction_count(account.address, "pending")
    if pending != nonce and not (pending == nonce + 1 and known is not None):
        raise ValueError("CREATE_GROUP_NONCE_CLOSE_CONFLICT")
    if any(w3.eth.get_code(account.address, block_identifier=tag) for tag in (latest.number, "pending")):
        raise ValueError("CREATE_GROUP_NONCE_CLOSE_ACCOUNT_CODE")
    tx = dict(chainId=int(proposal["chain_id"]), nonce=nonce, to=account.address, gas=GAS, value=0, data="0x", type=2,
              maxFeePerGas=int(proposal["max_fee_per_gas_wei"]),
              maxPriorityFeePerGas=int(proposal["max_priority_fee_per_gas_wei"]))
    check_transaction_gas(w3, tx)
    if tx["maxFeePerGas"] < latest.baseFeePerGas + tx["maxPriorityFeePerGas"]:
        raise ValueError("CREATE_GROUP_NONCE_CLOSE_FEE")
    if w3.eth.get_balance(account.address, block_identifier=latest.number) < int(result["nonce_close"]["max_cost_wei"]):
        raise ValueError("CREATE_GROUP_NONCE_CLOSE_FUNDING")
    if (w3.eth.get_block(latest.number).hash != latest.hash
            or w3.eth.get_block(block.number).hash != block.hash):
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    if confirmed is None:
        status = "CREATE_GROUP_NONCE_CLOSE_SAVED" if saved else "CREATE_GROUP_NONCE_CLOSE_REVIEW"
        return dict(status=status, **result)
    if (w3.eth.chain_id != tx["chainId"] or w3.eth.get_transaction_count(account.address, "latest") != nonce
            or w3.eth.get_transaction_count(account.address, "pending") != pending):
        raise ValueError("CREATE_GROUP_NONCE_CLOSE_CONFLICT")
    if saved is None:
        signed = account.sign_transaction(tx)
        saved = dict(expected, proposal=proposal, raw_transaction=signed.raw_transaction.hex(),
                     tx_hash=signed.hash.hex())
        validate_close(saved, expected, account)
        lane.put(key, saved)
    try:
        bootstrap.broadcast(saved["raw_transaction"])
    except Exception as error:
        # The provider may have accepted the bytes before losing its response.
        # Require this same operator confirmation for any retry.
        emit_debug(error, code="CREATE_GROUP_NONCE_CLOSE_SEND_UNCERTAIN")
        return dict(status="CREATE_GROUP_NONCE_CLOSE_SEND_UNCERTAIN",
                    **dict(result, tx_hash=saved["tx_hash"]))
    return dict(status="CREATE_GROUP_NONCE_CLOSE_BROADCAST", **dict(result, tx_hash=saved["tx_hash"]))


def close_outcomes(bootstrap, lane, account, block, group):
    from .bootstrap_recovery import receipt_at

    results = {}
    for attempt in group:
        key = close_key(attempt)
        saved = reserved_close(lane, key, binding(bootstrap, account, attempt, group), account)
        if saved is None:
            continue
        receipt = receipt_at(bootstrap.w3, saved, block)
        result = dict(tx_hash=saved["tx_hash"], nonce=attempt["nonce"], receipt_status="REPLACED")
        if receipt is not None:
            result.update(receipt_status=str(receipt.status), block_hash=receipt.blockHash.hex(),
                          block_number=str(receipt.blockNumber))
        results[key] = result
    return results

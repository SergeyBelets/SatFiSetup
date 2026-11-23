"""Review and retry a journaled EIP-1559 call at its existing nonce."""

import fcntl
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from hexbytes import HexBytes
from web3 import Web3
from .evm_rpc import evm_web3
from web3.exceptions import TransactionNotFound

from .bootstrap_nonce_close import GAS as SELF_TRANSFER_GAS, minimum_fees
from .bootstrap_recovery import anchor, identity_lane, protected_nonce_floor, signed_fields
from .evm_network import MAX_TRANSACTION_GAS, read_block, transaction_policy
from .operations import Journal, canonical, public_data
from .safe_errors import emit_debug

ALTERNATIVE = "FEE_REPLACEMENT_CANDIDATE"
TRACKING = "FEE_REPLACEMENT_PENDING"
SUPERSEDED = "FEE_REPLACEMENT_SUPERSEDED"
LIMIT = 8
SIGNED_KEYS = ("tx_hash", "raw_transaction", "nonce", "transfer_balance_before")
CALL_KEYS = ("type", "chainId", "nonce", "to", "value", "data", "accessList")


def _steps(journal, original):
    from .bootstrap_parallel import operation_binding

    entry = journal.get(original["operation"])
    step = entry["request"]
    suffix = ":" + step["id"]
    if not original["operation"].endswith(suffix):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    prefix = original["operation"][:-len(suffix)]
    profile = journal.get(prefix)["request"]["profile"]
    binding = operation_binding(SimpleNamespace(journal=journal), entry["id"], step)
    if (profile.get("deployment_id") != prefix or str(profile.get("chain_id")) != original["chain_id"]
            or not any(item == step for item in profile["steps"])
            or any(original.get(key) != value for key, value in binding.items())):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    return entry, profile


def _eligible_call(entry, fields):
    step = entry["request"]
    return (step["kind"] == "call" and not step.get("parallel_group") and not step.get("create_group")
            and fields.get("type") == 2 and len(fields["to"]) == 20 and bool(fields["data"]))


def _eligible(original, entry, profile, fields):
    if not _eligible_call(entry, fields):
        return False
    step = entry["request"]
    if step["id"] == "execute" and profile["deployment_id"].startswith("operation-"):
        return False  # The API worker must revalidate its current policy.
    if original.get("allow_fee_replacement") is True:
        return True
    from .reservation_transfer_cli import is_reservation_transfer
    if is_reservation_transfer(profile["deployment_id"], step):
        return True
    # Older FROST signer calls predate the explicit marker. Identify the
    # exact coordinator envelope; other historical budgeted callers opt out.
    if (step["id"] != "send" or set(profile["contracts"]) != {"target"}
            or profile["steps"] != [step] or step["contract"] != "target"
            or profile["contracts"]["target"]["artifact"] != "FROSTCoordinator"):
        return False
    target = profile["contracts"]["target"]["address"]
    operation = Web3.keccak(text=canonical([target, step["method"], step["args"]])).hex()
    return (profile["deployment_id"] == "signer-" + operation and fields["to"] == HexBytes(target))


def _candidate_rows(lane, tx_hash):
    with lane.db() as db:
        return db.execute("SELECT id,request FROM operations WHERE json_extract(request,'$.replacement_root')=? "
                          "ORDER BY rowid LIMIT ?", (tx_hash, LIMIT + 1)).fetchall()


def attempts(original, rows, account=None):
    """Decode and bind every alternative, including the immutable first attempt."""
    if len(rows) > LIMIT:
        raise ValueError("EVM_REPLACEMENT_LIMIT")
    sender = account
    if sender is None:
        from eth_account import Account
        sender = SimpleNamespace(address=Account.recover_transaction(HexBytes(original["raw_transaction"])))
    chain_id = int(original["chain_id"])
    fields = signed_fields(original, sender, chain_id)
    lineage = [(original, fields)]
    predecessor = original["tx_hash"]
    for row in rows:
        saved = json.loads(row["request"])
        candidate = signed_fields(saved, sender, chain_id)
        review = saved["replacement_review"]
        cancel = review.get("cancel_reservation") is True
        if cancel:
            from .reservation_transfer_cli import PREFIX
            if (not original["operation"].startswith(PREFIX) or not original["operation"].endswith(":transfer")
                    or candidate.get("type") != 2 or candidate["nonce"] != fields["nonce"]
                    or candidate["to"] != HexBytes(sender.address) or candidate["data"] or candidate["value"]
                    or candidate["gas"] != SELF_TRANSFER_GAS or candidate["accessList"]):
                raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        elif (lineage[-1][0].get("replacement_review", {}).get("cancel_reservation")
                or any(candidate.get(key) != fields.get(key) for key in CALL_KEYS)):
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        if (row["id"] != saved["tx_hash"] or saved["replacement_previous"] != predecessor
                or any(saved.get(key) != original.get(key) for key in (
                    "source_journal", "operation", "step_hash", "nonce", "transfer_balance_before"))
                or _review(original, lineage[-1][0], fields, candidate, sender,
                           review["max_cost_wei"], cancel=cancel) != review):
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        _check_increase(lineage[-1][1], candidate, cancel=cancel)
        lineage.append((saved, candidate))
        predecessor = saved["tx_hash"]
    return lineage


def _receipts(w3, lineage, latest):
    mined = []
    for saved, tx in lineage:
        try:
            receipt = w3.eth.get_transaction_receipt(saved["tx_hash"])
        except TransactionNotFound:
            continue
        if receipt.blockNumber > latest.number:
            raise ValueError("EVM_RECEIPT_HEAD_BEHIND")
        if (receipt.transactionHash != HexBytes(saved["tx_hash"]) or receipt.status not in (0, 1)
                or receipt.blockNumber < 0
                or w3.eth.get_block(receipt.blockNumber).hash != receipt.blockHash
                or not 0 < receipt.gasUsed <= tx["gas"]
                or not 0 <= receipt.effectiveGasPrice <= tx["maxFeePerGas"]):
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        mined.append((saved, receipt))
    if len(mined) > 1 or w3.eth.get_block(latest.number).hash != latest.hash:
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    return mined[0] if mined else (None, None)


def _check_increase(previous, candidate, *, cancel=False):
    fee, priority = minimum_fees(previous)
    if (candidate["maxFeePerGas"] < fee or candidate["maxPriorityFeePerGas"] < priority
            or candidate["maxPriorityFeePerGas"] > candidate["maxFeePerGas"]
            or (not cancel and candidate["gas"] < previous["gas"])):
        raise ValueError("EVM_REPLACEMENT_FEE_INCREASE")


def _review(original, previous, fields, candidate, account, maximum, *, cancel=False):
    call = candidate if cancel else fields
    review = dict(chain_id=original["chain_id"], sender=account.address, nonce=original["nonce"],
                operation=original["operation"], original_tx_hash=original["tx_hash"],
                previous_tx_hash=previous["tx_hash"], to=Web3.to_checksum_address(call["to"].hex()),
                value=str(call["value"]), data_hash=Web3.keccak(call["data"]).hex(),
                old_gas=str(fields["gas"]), gas=str(candidate["gas"]),
                max_fee_per_gas_wei=str(candidate["maxFeePerGas"]),
                max_priority_fee_per_gas_wei=str(candidate["maxPriorityFeePerGas"]),
                worst_case_wei=str(candidate["gas"] * candidate["maxFeePerGas"] + call["value"]),
                max_cost_wei=str(maximum))
    if cancel:
        review["cancel_reservation"] = True
    return review


def _fingerprint(review):
    return hashlib.sha256(canonical(review).encode()).hexdigest()


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,77}", value) or int(value) >= 2**256:
        raise ValueError("EVM_REPLACEMENT_BUDGET_REQUIRED")
    return int(value)


def _preflight(w3, account, tx, maximum, latest, known_hash, lineage, *, cancel=False):
    caps = transaction_policy(w3)
    from .evm_network import _limit

    gross = tx["gas"] * tx["maxFeePerGas"] + tx["value"]
    if (gross > maximum or tx["gas"] > min(MAX_TRANSACTION_GAS, latest.gasLimit)
            or caps and (tx["gas"] > _limit("SATOSHIFI_MAX_TX_GAS")
                         or any(tx[key] > limit for key, limit in caps.items()))):
        raise ValueError("EVM_REPLACEMENT_BUDGET_EXCEEDED")
    if tx["maxFeePerGas"] < latest.baseFeePerGas + tx["maxPriorityFeePerGas"]:
        raise ValueError("EVM_REPLACEMENT_BASE_FEE")
    if w3.eth.get_balance(account.address, block_identifier=latest.number) < gross:
        raise ValueError("EVM_REPLACEMENT_FUNDING")
    if (w3.eth.get_code(account.address, block_identifier=latest.number)
            or w3.eth.get_code(account.address, block_identifier="pending")):
        raise ValueError("EVM_REPLACEMENT_ACCOUNT_CODE")
    if cancel:
        # A zero-value self-transfer to the checked code-free sender has
        # fixed intrinsic gas. RPC estimators may pad even an empty call.
        if (tx["to"] != HexBytes(account.address) or tx["data"] or tx["value"]
                or tx["gas"] != SELF_TRANSFER_GAS or tx["accessList"]):
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    else:
        call = {key: tx[key] for key in ("value", "maxFeePerGas", "maxPriorityFeePerGas")}
        call.update({"from": account.address, "to": Web3.to_checksum_address(tx["to"].hex()),
                     "data": Web3.to_hex(tx["data"]), "accessList": [
                         {"address": Web3.to_checksum_address(item["address"].hex()),
                          "storageKeys": [f"0x{word:064x}" for word in item["storageKeys"]]} for item in tx["accessList"]]})
        if w3.eth.estimate_gas(call, block_identifier=latest.number) > tx["gas"]:
            raise ValueError("EVM_REPLACEMENT_GAS_LIMIT")
    if w3.eth.get_block(latest.number).hash != latest.hash:
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    if w3.eth.get_transaction_count(account.address, "latest") != tx["nonce"]:
        raise ValueError("EVM_REPLACEMENT_NONCE_CONFLICT")
    pending = w3.eth.get_transaction_count(account.address, "pending")
    if pending < tx["nonce"] or pending > tx["nonce"] and not any(
            known_hash(saved["tx_hash"]) for saved, _ in lineage):
        raise ValueError("EVM_REPLACEMENT_NONCE_CONFLICT")


def replace_pending(identity, rpc, tx_hash, *, fee, priority, max_cost, gas=None, confirmed=None, cancel=False,
                    api_guard=None):
    """Fingerprint immutable call + caps before creating a signed alternative."""
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", tx_hash or ""):
        raise ValueError("EVM_REPLACEMENT_TRANSACTION")
    tx_hash = HexBytes(tx_hash).hex()
    account, lock, lane = identity_lane(identity, require_existing=True)
    try:
        requested = lane.get(tx_hash)["request"]
        tx_hash = requested.get("replacement_root", tx_hash)
        original = lane.get(tx_hash)["request"]
        if set(original) == {"chain_id", "tx_hash", "raw_transaction"}:
            raise ValueError("EVM_LEGACY_NONCE_SOURCE_REQUIRED")
        path = Path(original["source_journal"])
        if not path.is_file():
            raise ValueError("EVM_REPLACEMENT_NOT_JOURNALED")
        journal = Journal(path)
    except KeyError:
        raise ValueError("EVM_REPLACEMENT_NOT_JOURNALED") from None
    # Use the same lock order as Bootstrap.apply so the old call cannot be
    # re-broadcast or consume a different nonce during approval and reservation.
    with Path(str(journal.path) + ".lock").open("a") as source_lock, lock.open("a") as nonce_lock:
        fcntl.flock(source_lock, fcntl.LOCK_EX)
        fcntl.flock(nonce_lock, fcntl.LOCK_EX)
        original = lane.get(tx_hash)["request"]
        if original["tx_hash"] != tx_hash or "replacement_root" in original:
            raise ValueError("EVM_REPLACEMENT_TRANSACTION")
        entry, profile = _steps(journal, original)
        w3 = evm_web3(rpc, timeout=20)
        chain = w3.eth.chain_id
        if chain != int(original["chain_id"]):
            raise ValueError("CHAIN_MISMATCH")
        if chain == 1:
            read_block(w3, int(profile.get("evm_confirmations", "1")))
        lineage = attempts(original, _candidate_rows(lane, tx_hash), account)
        fields = lineage[0][1]
        from .reservation_transfer_cli import bound_review, is_reservation_transfer, validate_rebroadcast
        prefix, step = profile["deployment_id"], entry["request"]
        reservation = is_reservation_transfer(prefix, step)
        if cancel and (not reservation or gas is not None):
            raise ValueError("EVM_REPLACEMENT_CANCEL_UNSUPPORTED")
        if api_guard is None and not _eligible(original, entry, profile, fields):
            raise ValueError("EVM_REPLACEMENT_UNSUPPORTED")
        if api_guard is not None:
            if (cancel or gas is not None or not _eligible_call(entry, fields)
                    or any(candidate["gas"] != fields["gas"] for _, candidate in lineage)):
                raise ValueError("EVM_REPLACEMENT_UNSUPPORTED")
        latest = w3.eth.get_block("latest")
        winner, receipt = _receipts(w3, lineage, latest)
        if winner is not None:
            if api_guard is not None:
                api_guard(w3, journal, entry, profile, fields)
            cancelled = winner.get("replacement_review", {}).get("cancel_reservation")
            if reservation and winner["tx_hash"] != original["tx_hash"]:
                # Reconcile a mined alternative even when the ceremony no
                # longer passes preflight. In particular its revert can then
                # be acknowledged at the exact, finalized nonce.
                status = lane.get(tx_hash)["status"]
                outcome = (observe(SimpleNamespace(w3=w3), lane, account, original, journal, entry,
                                   int(profile.get("evm_confirmations", "1")))
                           if status == TRACKING else {"status": "CANCELLED" if cancelled else "WINNER"})
                if cancelled:
                    return dict(status="RESERVATION_TRANSFER_CANCELLED" if outcome["status"] == "CANCELLED"
                                else "RESERVATION_TRANSFER_CANCEL_CONFIRMING", tx_hash=winner["tx_hash"],
                                block_number=str(receipt.blockNumber))
            return dict(status="EVM_REPLACEMENT_ALREADY_MINED", tx_hash=winner["tx_hash"],
                        receipt_status=str(receipt.status), block_number=str(receipt.blockNumber))
        if (entry["status"] not in ("SIGNED", "BROADCAST", "PREPARED") or entry["result"].get("block_hash")
                or lane.get(tx_hash)["status"] not in ("PREPARED", TRACKING)):
            raise ValueError("EVM_REPLACEMENT_NOT_PENDING")
        if any(entry["result"][key] != original[key] for key in SIGNED_KEYS if key in entry["result"]):
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        nonce = fields["nonce"]
        if nonce < protected_nonce_floor(w3, lane, account, chain):
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        from .bootstrap_parallel import verify_source_anchors

        if cancel:
            memo, _ = bound_review(w3, journal, prefix, step, fields, account.address)
            read_block(w3, int(memo["confirmations"]))
        else:
            verify_source_anchors(w3, journal, prefix)
        previous, last = lineage[-1]
        if not cancel and previous.get("replacement_review", {}).get("cancel_reservation"):
            raise ValueError("EVM_REPLACEMENT_CANCEL_PENDING")
        saved = previous if len(lineage) > 1 and previous.get("replacement_review") and (
            confirmed == _fingerprint(previous["replacement_review"])) else None
        if saved:
            last_review = saved["replacement_review"]
            previous, last = lineage[-2]
        else:
            last_review = None
        tx = {key: fields[key] for key in CALL_KEYS}
        tx.update(gas=_number(gas) if gas is not None else last["gas"],
                  maxFeePerGas=_number(fee), maxPriorityFeePerGas=_number(priority))
        if cancel:
            tx.update(to=HexBytes(account.address), value=0, data=HexBytes("0x"),
                      gas=SELF_TRANSFER_GAS, accessList=[])
        maximum = _number(max_cost)
        _check_increase(last, tx, cancel=cancel)
        review = _review(original, previous, fields, tx, account, maximum, cancel=cancel)
        fingerprint = _fingerprint(review)
        if confirmed is not None and confirmed != fingerprint or last_review is not None and last_review != review:
            raise ValueError("EVM_REPLACEMENT_FINGERPRINT")
        if not saved and len(lineage) > LIMIT:
            raise ValueError("EVM_REPLACEMENT_LIMIT")
        # API calls inherit the original deployment profile, including the
        # formation deadline. Their current policy and call guard govern retries.
        if (api_guard is None and profile.get("approval_deadline") is not None
                and latest.timestamp >= int(profile["approval_deadline"])):
            raise ValueError("EVM_REPLACEMENT_APPROVAL_EXPIRED")

        def pending_hash(candidate_hash):
            try:
                known = w3.eth.get_transaction(candidate_hash)
            except TransactionNotFound:
                return False
            if known["hash"] != HexBytes(candidate_hash):
                raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
            if known["blockNumber"] is not None:
                raise ValueError("EVM_REPLACEMENT_NONCE_CONFLICT")
            return True

        if not saved or not pending_hash(saved["tx_hash"]):
            _preflight(w3, account, tx, maximum, latest, pending_hash, lineage, cancel=cancel)
            if reservation and not cancel:
                validate_rebroadcast(w3, journal, prefix, step, fields, account.address)
        if api_guard is not None:
            api_guard(w3, journal, entry, profile, fields)
        if confirmed is None:
            return dict(status="EVM_REPLACEMENT_REVIEW", replacement=review, fingerprint=fingerprint)
        if saved is None:
            signed = account.sign_transaction(tx)
            saved = dict(original, tx_hash=signed.hash.hex(), raw_transaction=signed.raw_transaction.hex(),
                         replacement_root=tx_hash, replacement_previous=previous["tx_hash"], replacement_review=review)
            _save_candidate(lane, saved)
        if pending_hash(saved["tx_hash"]):
            return dict(status="EVM_REPLACEMENT_BROADCAST", tx_hash=saved["tx_hash"], fingerprint=fingerprint)
        try:
            from .bootstrap import Bootstrap

            Bootstrap.broadcast(SimpleNamespace(w3=w3), saved["raw_transaction"])
        except Exception as error:
            from .participant_errors import participant_message
            status = "EVM_REPLACEMENT_SEND_UNCERTAIN"
            emit_debug(error, code=status)
            return dict(status=status, tx_hash=saved["tx_hash"], fingerprint=fingerprint,
                        message=participant_message(status))
        return dict(status="EVM_REPLACEMENT_BROADCAST", tx_hash=saved["tx_hash"], fingerprint=fingerprint)


def _save_candidate(lane, saved):
    body = canonical(public_data(saved))
    with lane.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO operations(id,digest,request,status) VALUES(?,?,?,?)",
                   (saved["tx_hash"], hashlib.sha256(body.encode()).hexdigest(), body, ALTERNATIVE))
        db.execute("INSERT INTO events(operation,status) VALUES(?,?)", (saved["tx_hash"], ALTERNATIVE))
        db.execute("UPDATE operations SET status=? WHERE id=?", (TRACKING, saved["replacement_root"]))
        db.execute("INSERT INTO events(operation,status) VALUES(?,?)", (saved["replacement_root"], TRACKING))


def is_related(lane, replacement, previous_result):
    """Handle a crash between recording the winning nonce and its source result."""
    tx_hash = replacement.get("replacement_root")
    if not tx_hash:
        return False
    row = lane.get(tx_hash)
    original = row["request"]
    return (row["status"] == SUPERSEDED and row["result"]["tx_hash"] == replacement["tx_hash"]
            and all(original.get(field) == previous_result.get(field) for field in SIGNED_KEYS)
            and any(item[0]["tx_hash"] == replacement["tx_hash"]
                    for item in attempts(original, _candidate_rows(lane, tx_hash))))


def observe(bootstrap, lane, account, original, source, entry, replacement_floor):
    """Reconcile the original or alternative winner under the identity lock."""
    rows = _candidate_rows(lane, original["tx_hash"])
    if not rows:
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    lineage = attempts(original, rows, account)
    w3 = bootstrap.w3
    latest = w3.eth.get_block("latest")
    winner, receipt = _receipts(w3, lineage, latest)
    if winner is not None:
        if winner["tx_hash"] == original["tx_hash"]:
            return {"status": "ORIGINAL"}
        _, profile = _steps(source, original)
        block = read_block(w3, max(replacement_floor, int(profile.get("evm_confirmations", "1"))))
        # Match the finalized boundary used to protect retired nonces. read_block
        # already applies it on mainnet. The local chain only uses depth.
        if w3.eth.chain_id not in (1, 31337):
            block = min(block, anchor(w3), key=lambda item: item.number)
        if receipt.blockNumber <= block.number:
            if (w3.eth.get_block(receipt.blockNumber).hash != receipt.blockHash
                    or w3.eth.get_transaction_count(lineage_sender(original, account), block.number) <= int(original["nonce"])
                    or w3.eth.get_block(block.number).hash != block.hash):
                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
            evidence = dict(tx_hash=winner["tx_hash"], nonce=original["nonce"],
                            finalized_block_number=str(receipt.blockNumber), finalized_block_hash=receipt.blockHash.hex())
            cancel = winner.get("replacement_review", {}).get("cancel_reservation") is True
            next_status, next_result = "PREPARED", {}
            if cancel:
                if receipt.status != 1:
                    raise ValueError("EVM_REPLACEMENT_CANCEL_REVERTED")
                source.update(entry["id"], "CANCELLED", dict(
                    original_tx_hash=original["tx_hash"], cancellation_tx_hash=winner["tx_hash"],
                    block_number=str(receipt.blockNumber), block_hash=receipt.blockHash.hex()))
                next_status = "CONFIRMED"
                next_result = dict(block_number=str(receipt.blockNumber), block_hash=receipt.blockHash.hex())
            with lane.db() as db:
                db.execute("BEGIN IMMEDIATE")
                for key, status, result in ((original["tx_hash"], SUPERSEDED, evidence),
                                             (winner["tx_hash"], next_status, next_result)):
                    db.execute("UPDATE operations SET status=?,result=? WHERE id=?", (status, canonical(result), key))
                    db.execute("INSERT INTO events(operation,status) VALUES(?,?)", (key, status))
            if cancel:
                return {"status": "CANCELLED"}
            source.update(entry["id"], "SIGNED", {key: winner[key] for key in SIGNED_KEYS if key in winner})
            return {"status": "WINNER", "saved": winner}
    if (winner is None and account is not None
            and w3.eth.get_transaction_count(account.address, latest.number) > int(original["nonce"])):
        raise ValueError("EVM_REPLACEMENT_NONCE_CONFLICT")
    return dict(status="BROADCAST", tx_hash=(winner or lineage[-1][0])["tx_hash"],
                original_tx_hash=original["tx_hash"], reason="EVM_REPLACEMENT_PENDING")


def lineage_sender(original, account):
    if account is not None:
        return account.address
    from eth_account import Account

    return Account.recover_transaction(HexBytes(original["raw_transaction"]))

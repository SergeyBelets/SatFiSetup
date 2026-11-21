"""Explicit recovery with canonical receipt and nonce evidence."""
import fcntl
import hashlib
import json
from pathlib import Path

from eth_account import Account
from eth_account._utils.legacy_transactions import Transaction
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound

from .onboarding import read_private
from .operations import Journal, canonical


def identity_lane(identity, *, require_existing=False):
    account = Account.from_key(json.loads(read_private(identity))["private_key"])
    lock = nonce_lock_path(identity)
    journal = Path(str(lock) + ".sqlite")
    if require_existing and not journal.is_file():
        raise ValueError("CREATE_GROUP_RECOVERY_JOURNAL")
    return account, lock, Journal(journal)


def nonce_lock_path(identity):
    return Path(identity).resolve().with_suffix(".evm-nonce.lock")


def require_resume_lane(bootstrap, identity, account):
    """A recovered plan stays bound to the original nonce lane across resumes."""
    journal = bootstrap.journal
    with journal.db() as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='withdrawal_resume_guard'").fetchone()
        row = db.execute("SELECT binding FROM withdrawal_resume_guard WHERE deployment=?",
                         (bootstrap.prefix,)).fetchone() if table else None
    proof = json.loads(row["binding"]) if row else journal.get(bootstrap.prefix)["result"].get("withdrawal_recovery")
    if proof is None:
        return
    lane_path = Path(str(nonce_lock_path(identity)) + ".sqlite")
    if (proof["operator"] != account.address or proof["chain_id"] != str(bootstrap.w3.eth.chain_id)
            or "lane_path" in proof and proof["lane_path"] != str(lane_path)
            or not lane_path.is_file()):
        raise ValueError("CREATE_GROUP_RECOVERY_IDENTITY_LANE")
    lane = Journal(lane_path)
    try:
        for attempt in proof["attempts"]:
            row = lane.get(attempt["tx_hash"])
            if row["status"] != "RETIRED" or row["request"].get("tx_hash") != attempt["tx_hash"]:
                raise ValueError("CREATE_GROUP_RECOVERY_IDENTITY_LANE")
    except KeyError:
        raise ValueError("CREATE_GROUP_RECOVERY_IDENTITY_LANE") from None


def anchor(w3):
    block = w3.eth.get_block("latest" if w3.eth.chain_id == 31337 else "finalized")
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    return block


def signed_fields(saved, account, chain_id):
    try:
        raw = HexBytes(saved["raw_transaction"])
        if (not raw or Web3.keccak(raw) != HexBytes(saved["tx_hash"])
                or saved.get("chain_id", str(chain_id)) != str(chain_id)
                or (account is not None and Account.recover_transaction(raw) != account.address)):
            raise ValueError()
        if raw[0] < 0x80:
            fields = TypedTransaction.from_bytes(raw).as_dict()
            actual_chain = fields["chainId"]
        else:
            fields = Transaction.from_bytes(raw).as_dict()
            actual_chain = (fields["v"] - 35) // 2
        if actual_chain != chain_id or ("nonce" in saved and int(saved["nonce"]) != fields["nonce"]):
            raise ValueError()
    except Exception:
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING") from None
    return fields


def signed_nonce(saved, account, chain_id):
    return signed_fields(saved, account, chain_id)["nonce"]


def protected_nonce_floor(w3, lane, account, chain_id):
    """Revalidate finalized evidence before reusing a lane with closed nonces."""
    with lane.db() as db:
        rows = db.execute(
            "SELECT id,request,result,status FROM operations WHERE json_extract(request,'$.chain_id')=? "
            "AND (status='RETIRED' OR status='CONFIRMED' AND json_extract(result,'$.receipt_status')='0')",
            (str(chain_id),)).fetchall()
    if not rows:
        return 0
    from .bootstrap_nonce_close import CLOSE_KIND

    finalized = anchor(w3)
    hashes = {finalized.number: finalized.hash}
    retired = {row["id"]: json.loads(row["result"]) for row in rows if row["status"] == "RETIRED"}
    sender, floor = None, 0
    for row in rows:
        saved, result = json.loads(row["request"]), json.loads(row["result"])
        nonce = signed_nonce(saved, account, chain_id)
        address = Account.recover_transaction(HexBytes(saved["raw_transaction"])) if account is None else account.address
        if (sender is not None and sender != address or int(result.get("nonce", nonce)) != nonce
                or saved.get("kind") != CLOSE_KIND and row["id"] != saved["tx_hash"]):
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        sender = address
        if row["status"] == "RETIRED":
            if saved.get("kind") == CLOSE_KIND:
                # Older SDKs kept the consumption anchor on the original CREATE.
                witness = retired.get(saved["original_tx_hash"])
                if witness is None or int(witness["nonce"]) != nonce:
                    raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
            else:
                witness = result
            number, block_hash = int(witness["finalized_block_number"]), witness["finalized_block_hash"]
        else:
            number, block_hash = int(result["block_number"]), result["block_hash"]
        if not 0 <= number <= finalized.number:
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        if number not in hashes:
            hashes[number] = w3.eth.get_block(number).hash
        if hashes[number] != HexBytes(block_hash):
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        floor = max(floor, nonce + 1)
    if (w3.eth.get_transaction_count(sender, finalized.number) < floor
            or w3.eth.get_block(finalized.number).hash != finalized.hash):
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    return floor


def receipt_at(w3, saved, block):
    try:
        receipt = w3.eth.get_transaction_receipt(saved["tx_hash"])
    except TransactionNotFound:
        return None
    if (receipt.transactionHash != HexBytes(saved["tx_hash"]) or receipt.status not in (0, 1)
            or not 0 <= receipt.blockNumber <= block.number
            or w3.eth.get_block(receipt.blockNumber).hash != receipt.blockHash):
        raise ValueError("RECOVERY_RECEIPT_NOT_FINALIZED")
    return receipt


def check_receipt_binding(receipt, result):
    if getattr(receipt, "transactionHash", None) != HexBytes(result["tx_hash"]) or receipt.status not in (0, 1):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    if (result.get("block_hash") and HexBytes(result["block_hash"]) != receipt.blockHash
            or result.get("block_number") and int(result["block_number"]) != receipt.blockNumber):
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")


def acknowledge_revert(identity, tx_hash, rpc):
    account, lock, lane = identity_lane(identity)
    tx_hash = HexBytes(tx_hash).hex()
    with lock.open("a") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        entry = lane.get(tx_hash)
        saved = entry["request"]
        if entry["status"] == "RETIRED":
            raise ValueError("CREATE_GROUP_RECOVERY_REQUIRED")
        if "source_journal" in saved:
            step = Journal(saved["source_journal"]).get(saved["operation"])["request"]
            if step.get("create_group"):
                raise ValueError("CREATE_GROUP_RECOVERY_REQUIRED")
            if hashlib.sha256(canonical(step).encode()).hexdigest() != saved["step_hash"]:
                raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
        protected_nonce_floor(w3, lane, account, w3.eth.chain_id)
        nonce = signed_nonce(saved, account, w3.eth.chain_id)
        block = anchor(w3)
        receipt = receipt_at(w3, saved, block)
        if receipt is None or receipt.status != 0 or w3.eth.get_transaction_count(account.address, block.number) <= nonce:
            raise ValueError("RECOVERY_REVERT_NOT_FINALIZED")
        if w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        result = dict(tx_hash=tx_hash, nonce=str(nonce), receipt_status="0",
                      block_number=str(receipt.blockNumber), block_hash=receipt.blockHash.hex())
        lane.update(tx_hash, "CONFIRMED", result)
        return dict(status="REVERT_ACKNOWLEDGED", **result)


def recheck_receipt(bootstrap, step_id):
    """Queue a marked transaction for observation without signing or sending."""
    key = bootstrap.prefix + ":" + step_id
    with Path(str(bootstrap.journal.path) + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entry = bootstrap.journal.get(key)
        result = entry["result"]
        if (entry["status"] != "RECOVERY_REQUIRED" or not result.get("tx_hash")
                or not result.get("block_hash") or not result.get("block_number")):
            raise ValueError("RECOVERY_RECHECK_NOT_APPLICABLE")
        if bootstrap.verify_receipt(result) is None:
            raise ValueError("RECOVERY_RECEIPT_NOT_FINALIZED")
        bootstrap.journal.update(key, "CONFIRMING", result)
        return dict(status="RECHECK_QUEUED", step=step_id, tx_hash=result["tx_hash"])


def previous_steps(bootstrap, steps, block, entries):
    """Validate the prefix that resume will reuse before spending any gas."""
    from .bootstrap import artifact, artifact_fingerprint

    w3 = bootstrap.w3
    hashes, artifacts, transactions = {block.number: block.hash}, {}, {}
    for step in steps:
        key = bootstrap.prefix + ":" + step["id"]
        entry = entries[key]
        result = entry["result"]
        if entry["request"] != step:
            raise ValueError("CREATE_GROUP_RECOVERY_JOURNAL")
        if step["kind"] == "bitcoin_ready" and entry["status"] == "ZK_FRESHNESS_DEFERRED":
            continue
        if entry["status"] != "CONFIRMED":
            raise ValueError("CREATE_GROUP_RECOVERY_PRIOR_STEP")
        anchors = []
        if step["kind"] in ("call", "deploy"):
            anchors.append((result["block_number"], result["block_hash"]))
            transactions[result["tx_hash"]] = key
        if "read_anchor" in result:
            read = result["read_anchor"]
            anchors.append((read["number"], read["hash"]))
        for number, block_hash in anchors:
            number = int(number)
            if not 0 <= number <= block.number:
                raise ValueError("RECOVERY_RECEIPT_NOT_FINALIZED")
            if number not in hashes:
                hashes[number] = w3.eth.get_block(number).hash
            if hashes[number] != HexBytes(block_hash):
                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        for item in [result.get("contract"), *result.get("outputs", {}).values()]:
            if isinstance(item, dict) and item.get("artifact_hash"):
                name = item["artifact"]
                pin = item["artifact_hash"]
                legacy = not pin.startswith("evm-v1:")
                artifact_key = (name, legacy)
                if artifact_key not in artifacts:
                    artifacts[artifact_key] = artifact_fingerprint(artifact(name), legacy=legacy)
                if pin != artifacts[artifact_key]:
                    raise ValueError("ARTIFACT_HASH_MISMATCH")
    return transactions


def group_attempts(bootstrap, lane, account, entries, steps, block, next_nonce):
    from .bootstrap_create import create_address
    from .bootstrap_parallel import operation_binding

    w3 = bootstrap.w3
    attempts, blocked = [], []
    base = None
    for entry, step in zip(entries, steps):
        result = entry["result"]
        if entry["request"] != step or entry["status"] not in ("SIGNED", "BROADCAST", "CONFIRMING", "REVERTED"):
            raise ValueError("CREATE_GROUP_RECOVERY_JOURNAL")
        row = lane.get(result["tx_hash"])
        saved = row["request"]
        if row["status"] == "RETIRED":
            retired = row["result"]
            number = int(retired["finalized_block_number"])
            if number > block.number or w3.eth.get_block(number).hash != HexBytes(retired["finalized_block_hash"]):
                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        binding = operation_binding(bootstrap, entry["id"], step)
        if (any(saved.get(field) != value for field, value in binding.items())
                or any(saved.get(field) != result.get(field) for field in ("tx_hash", "raw_transaction", "nonce"))):
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        fields = signed_fields(saved, account, w3.eth.chain_id)
        nonce = fields["nonce"]
        if base is None:
            base = nonce
        if fields["to"] or fields["value"] or not fields["data"] or nonce != base + len(attempts):
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        receipt = receipt_at(w3, saved, block)
        if receipt is not None and receipt.status != 0:
            raise ValueError("CREATE_GROUP_RECOVERY_SUCCESSFUL_DEPLOYMENT")
        if (receipt is None and (entry["status"] == "REVERTED" or result.get("block_hash"))
                or receipt is not None and (nonce >= next_nonce
                    or result.get("block_hash") and HexBytes(result["block_hash"]) != receipt.blockHash
                    or result.get("block_number") and int(result["block_number"]) != receipt.blockNumber)):
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        address = create_address(account.address, nonce)
        if w3.eth.get_code(address, block_identifier=block.number):
            raise ValueError("CREATE_GROUP_RECOVERY_SUCCESSFUL_DEPLOYMENT")
        status = "0" if receipt is not None else ("REPLACED" if nonce < next_nonce else "UNCONSUMED")
        attempts.append(dict(operation=entry["id"], tx_hash=saved["tx_hash"], nonce=str(nonce),
                             address=address, receipt_status=status))
        if nonce >= next_nonce:
            blocked.append((attempts[-1], fields))
    if not any(item["receipt_status"] == "0" for item in attempts):
        raise ValueError("CREATE_GROUP_RECOVERY_NO_REVERT")
    return attempts, blocked


def check_later_operations(bootstrap, lane, steps, earlier, attempts, close_keys, entries):
    later = set()
    for step in steps:
        key = bootstrap.prefix + ":" + step["id"]
        later.add(key)
        entry = entries.get(key)
        if entry is None:
            continue
        if entry["request"] != step or entry["status"] != "PREPARED" or entry["result"]:
            raise ValueError("CREATE_GROUP_RECOVERY_DEPENDENT_OPERATION")
    group_hashes = {item["tx_hash"] for item in attempts}
    with lane.db() as db:
        rows = db.execute(
            "SELECT id,status,json_extract(request,'$.operation') AS operation,"
            "json_extract(request,'$.source_journal') AS source FROM operations "
            "WHERE status != 'RETIRED' AND json_extract(request,'$.chain_id')=?",
            (str(bootstrap.w3.eth.chain_id),)).fetchall()
    source = str(bootstrap.journal.path.resolve())
    for row in rows:
        if row["source"] == source and row["operation"] in later:
            raise ValueError("CREATE_GROUP_RECOVERY_DEPENDENT_OPERATION")
        previous = row["id"] in earlier and row["source"] == source and earlier[row["id"]] == row["operation"]
        if row["status"] != "CONFIRMED" and row["id"] not in group_hashes | close_keys and not previous:
            raise ValueError("CREATE_GROUP_RECOVERY_IDENTITY_PENDING")


def recover_create_group(bootstrap, identity, accepted=None, *, prepare_close=False, confirm_close=None):
    """Check both journals and recover a failed withdrawal group in-place."""
    from .bootstrap_create import creation_groups
    from .bootstrap_nonce_close import close_nonce, close_key, close_outcomes

    if sum((accepted is not None, prepare_close, confirm_close is not None)) > 1:
        raise ValueError("RECOVERY_INPUTS_REQUIRED")
    account, lock, lane = identity_lane(identity, require_existing=True)
    label = "withdrawal-fee-constructors"
    plan = bootstrap.profile["steps"]
    steps = creation_groups(plan).get(label)
    if (not steps or account.address != bootstrap.profile.get("operator")
            or bootstrap.journal.path.resolve() == lane.path.resolve()
            or [(step["id"], step.get("artifact")) for step in steps] != [
                ("withdrawal_payments", "WithdrawalFeePayments"),
                ("withdrawal_funding", "WithdrawalFeeFunding"),
                ("withdrawal_credits", "WithdrawalFeeCredits")]):
        raise ValueError("CREATE_GROUP_RECOVERY_PROFILE")
    prefix = bootstrap.prefix + ":"
    w3 = bootstrap.w3
    with Path(str(bootstrap.journal.path) + ".lock").open("a") as source_lock, lock.open("a") as nonce_lock:
        fcntl.flock(source_lock, fcntl.LOCK_EX)
        fcntl.flock(nonce_lock, fcntl.LOCK_EX)
        source_entries = bootstrap.journal.steps_for(bootstrap.prefix)
        try:
            entries = [source_entries[prefix + step["id"]] for step in steps]
            if all(entry["status"] == "PREPARED" and not entry["result"] for entry in entries):
                root = bootstrap.journal.get(bootstrap.prefix)
                if root["status"] == "CREATE_GROUP_RETRY_READY":
                    ready = root["result"]["withdrawal_recovery"]
                    if accepted is not None and accepted != ready["fingerprint"]:
                        raise ValueError("CREATE_GROUP_RECOVERY_FINGERPRINT")
                    return ready
        except KeyError:
            raise ValueError("CREATE_GROUP_RECOVERY_JOURNAL") from None
        if not any(entry["status"] == "REVERTED" for entry in entries):
            raise ValueError("CREATE_GROUP_RECOVERY_NO_REVERT")
        block = anchor(w3)
        next_nonce = w3.eth.get_transaction_count(account.address, block.number)
        if next_nonce < protected_nonce_floor(w3, lane, account, w3.eth.chain_id):
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        try:
            earlier = previous_steps(bootstrap, plan[:plan.index(steps[0])], block, source_entries)
            retired, blocked = group_attempts(bootstrap, lane, account, entries, steps, block, next_nonce)
        except KeyError:
            raise ValueError("CREATE_GROUP_RECOVERY_JOURNAL") from None
        check_later_operations(bootstrap, lane, plan[plan.index(steps[-1]) + 1:], earlier, retired,
                               {close_key(item) for item in retired}, source_entries)
        if w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        if blocked:
            result = close_nonce(bootstrap, lane, account, block, next_nonce, *blocked[0],
                                 group=retired, prepare=prepare_close, confirmed=confirm_close)
            return dict(nonces=[item["nonce"] for item, _ in blocked], attempts=retired, **result)
        closed = close_outcomes(bootstrap, lane, account, block, retired)
        evidence = dict(deployment=bootstrap.prefix, operator=account.address, chain_id=str(w3.eth.chain_id),
                        attempts=retired, nonce_closures=list(closed.values()))
        fingerprint = hashlib.sha256(canonical(evidence).encode()).hexdigest()
        if accepted is None:
            return dict(status="CREATE_GROUP_RECOVERY_REVIEW", fingerprint=fingerprint, **evidence)
        if accepted != fingerprint:
            raise ValueError("CREATE_GROUP_RECOVERY_FINGERPRINT")
        if w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        # Retire the old bytes first. A crash here leaves the source stopped and
        # allows the identical recovery to finish. The source resets atomically.
        for item in retired:
            lane.update(item["tx_hash"], "RETIRED", dict(item, finalized_block_hash=block.hash.hex(),
                                                        finalized_block_number=str(block.number)))
        for key, result in closed.items():
            lane.update(key, "RETIRED" if result["receipt_status"] == "REPLACED" else "CONFIRMED", result)
        result = dict(status="CREATE_GROUP_RETRY_READY", group=label, fingerprint=fingerprint, **evidence)
        with bootstrap.journal.db() as db:
            for item in retired:
                db.execute("INSERT INTO events(operation,status) VALUES (?,?)", (item["operation"], "RETIRED"))
                db.execute("UPDATE operations SET status='PREPARED',result='{}' WHERE id=?", (item["operation"],))
            db.execute("CREATE TABLE IF NOT EXISTS withdrawal_resume_guard "
                       "(deployment TEXT PRIMARY KEY, binding TEXT NOT NULL)")
            db.execute("INSERT OR REPLACE INTO withdrawal_resume_guard(deployment,binding) VALUES (?,?)",
                       (bootstrap.prefix, canonical(dict(evidence, lane_path=str(lane.path.resolve())))))
            root = db.execute("SELECT result FROM operations WHERE id=?", (bootstrap.prefix,)).fetchone()
            root_result = dict(json.loads(root["result"]), withdrawal_recovery=result)
            db.execute("UPDATE operations SET status=?,result=? WHERE id=?",
                       (result["status"], canonical(root_result), bootstrap.prefix))
            db.execute("INSERT INTO events(operation,status) VALUES (?,?)", (bootstrap.prefix, result["status"]))
        return result

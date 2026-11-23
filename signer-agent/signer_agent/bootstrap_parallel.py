"""Bounded independent sends with one finality barrier and durable nonce ownership.

Only explicitly marked, contiguous groups execute ahead of confirmation. Group
outputs remain unavailable until every receipt and postcondition is verified.
"""
import hashlib
import json
import re
from pathlib import Path

from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound

from .operations import Journal, canonical

MAX_GROUP_STEPS = 8


def parallel_groups(steps):
    groups, ids, previous = {}, set(), None
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("id"), str) or step["id"] in ids:
            raise ValueError("BOOTSTRAP_STEP_ID")
        ids.add(step["id"])
        label = step.get("parallel_group")
        if label is None:
            previous = None
            continue
        if (not isinstance(label, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", label)
                or (label in groups and label != previous)):
            raise ValueError("BOOTSTRAP_PARALLEL_GROUP")
        group = groups.setdefault(label, [])
        group.append(step)
        previous = label
        if (len(group) > MAX_GROUP_STEPS or step.get("kind") not in ("deploy", "call")
                or step.get("outputs") or step.get("verify_token_transfer")):
            raise ValueError("BOOTSTRAP_PARALLEL_STEP")
    for group in groups.values():
        names = {step["id"] for step in group}
        for step in group:
            def references(value):
                if isinstance(value, str):
                    return value.startswith("$") and value[1:] in names
                if isinstance(value, list):
                    return any(references(item) for item in value)
                if isinstance(value, dict):
                    return any(references(item) for item in value.values())
                return False
            if (step.get("contract") in names or references(step.get("args", []))
                    or references(step.get("value"))):
                raise ValueError("BOOTSTRAP_PARALLEL_DEPENDENCY")
    return groups


def operation_binding(bootstrap, key, step):
    return dict(source_journal=str(bootstrap.journal.path.resolve()), operation=key,
                step_hash=hashlib.sha256(canonical(step).encode()).hexdigest())


def restore_signed(identity_journal, bootstrap, key, step, result=None, *, require_reserved=False):
    from .bootstrap_replacement import ALTERNATIVE, SUPERSEDED, is_related

    binding = operation_binding(bootstrap, key, step)
    with identity_journal.db() as db:
        rows = db.execute("SELECT id,request FROM operations WHERE status NOT IN ('RETIRED',?,?) AND json_extract(request, '$.source_journal')=? "
                          "AND json_extract(request, '$.operation')=?",
                          (ALTERNATIVE, SUPERSEDED, binding["source_journal"], key)).fetchall()
    if not rows:
        if result is not None and "raw_transaction" in result:
            try:
                existing = identity_journal.get(result["tx_hash"])
            except KeyError:
                existing = None
            # Only old single/parallel steps can predate the shared reservation.
            if require_reserved or existing and (
                    existing["status"] == "RETIRED" or "source_journal" in existing["request"]
                    or any(existing["request"].get(k) != result[k] for k in ("tx_hash", "raw_transaction"))):
                raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        return None
    if len(rows) != 1:
        raise ValueError("EVM_NONCE_CONFLICT_RECOVERY_REQUIRED")
    saved = json.loads(rows[0]["request"])
    if saved.get("tx_hash") != rows[0]["id"] or any(saved.get(field) != value for field, value in binding.items()):
        raise ValueError("IDEMPOTENCY_CONFLICT")
    from .bootstrap_recovery import signed_nonce
    signed_nonce(saved, None, int(saved["chain_id"]))
    envelope = {field: saved[field] for field in ("tx_hash", "raw_transaction", "nonce", "transfer_balance_before")
                if field in saved}
    if result is not None and "raw_transaction" in result and any(result.get(k) != v for k, v in envelope.items()):
        if result.get("block_hash") or not is_related(identity_journal, saved, result):
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    return envelope


def save_signed(identity_journal, bootstrap, key, step, result, *, allow_fee_replacement=False):
    """Reserve the nonce globally before updating the per-operation journal.

    A crash between these writes is recovered by restore_signed, never by
    allocating a new nonce or signing different bytes for the same operation.
    """
    request = dict(chain_id=str(bootstrap.w3.eth.chain_id),
        tx_hash=result["tx_hash"], raw_transaction=result["raw_transaction"],
        nonce=result["nonce"], **operation_binding(bootstrap, key, step))
    if allow_fee_replacement:
        request["allow_fee_replacement"] = True
    if "transfer_balance_before" in result:
        request["transfer_balance_before"] = result["transfer_balance_before"]
    try:
        existing = identity_journal.get(result["tx_hash"])
    except KeyError:
        identity_journal.put(result["tx_hash"], request)
    else:
        # Existing pre-scheduler journals contain only these three fields.
        fields = ("chain_id", "tx_hash", "raw_transaction")
        if "source_journal" in existing["request"]:
            fields += ("nonce", "source_journal", "operation", "step_hash")
        if existing["status"] == "RETIRED" or any(existing["request"].get(field) != request[field] for field in fields):
            raise ValueError("IDEMPOTENCY_CONFLICT")
    bootstrap.journal.update(key, "SIGNED", result)


def require_publication_context(bootstrap, source, prefix, step):
    """Only the owning worker can authorize sending its saved Bitcoin transition."""
    if (step.get("id") != "transition" or step.get("kind") != "call"
            or step.get("contract") != "relay" or step.get("method") != "submitTransition"
            or re.fullmatch(r"zk-worker-[0-9a-f]{64}", prefix) is None):
        return
    check = getattr(bootstrap, "check_worker_transition_broadcast", None)
    if check is None:
        raise ValueError("ZK_TRANSACTION_RECOVERY_REQUIRED")
    check(source, prefix, step)


def recover_identity(identity_journal, bootstrap, account=None, *, replacements=None, reservations=None):
    """Rebroadcast only authorized exact bytes, and stop on known failure/reorg."""
    from .bootstrap_nonce_close import CLOSE_KIND
    from .bootstrap_recovery import check_receipt_binding, protected_nonce_floor, signed_fields
    from .bootstrap_replacement import (ALTERNATIVE, SUPERSEDED, TRACKING, SIGNED_KEYS,
                                        is_related, lineage_sender, observe as replacement_status)
    from .finalization_recovery import is_finalization_step, reconcile
    from .evm_network import verify_anchor
    from .reservation_transfer_cli import is_reservation_transfer, validate_rebroadcast

    chain_id = bootstrap.w3.eth.chain_id
    floor = protected_nonce_floor(bootstrap.w3, identity_journal, account, chain_id)
    replacement_floor = 0
    source_journals = {}
    checked_sources = set()
    # Journal.list is a UI projection capped at 1000 rows. Recovery must never
    # lose an older reserved nonce just because many newer operations exist.
    with identity_journal.db() as db:
        rows = db.execute(
            "SELECT id,request,status,json_extract(request,'$.kind') AS kind FROM operations "
            "WHERE status NOT IN ('CONFIRMED','RETIRED',?,?) "
            "AND COALESCE(json_extract(request,'$.chain_id'),?)=? ORDER BY rowid",
            (ALTERNATIVE, SUPERSEDED, str(chain_id), str(chain_id))).fetchall()
    if any(row["kind"] == CLOSE_KIND for row in rows):
        raise ValueError("CREATE_GROUP_RECOVERY_REQUIRED")
    for row in rows:
        key = row["id"]
        saved = json.loads(row["request"])
        tx = signed_fields(saved, account, chain_id)
        nonce = tx["nonce"]
        if key != saved["tx_hash"] or saved.get("chain_id") != str(chain_id):
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        if reservations is not None:
            reservations.add(nonce)
        source_result = {}
        source_entry = None
        finalization = False
        if "source_journal" in saved:
            source = Path(saved["source_journal"])
            if source not in source_journals:
                if not source.is_file():
                    raise ValueError("PRIOR_TRANSACTION_RECOVERY_REQUIRED")
                source_journals[source] = Journal(source)
            source_journal = source_journals[source]
            source_entry = source_journal.get(saved["operation"])
            finalization = is_finalization_step(saved, source_entry)
            if (hashlib.sha256(canonical(source_entry["request"]).encode()).hexdigest() != saved["step_hash"]
                    or source_entry["status"] == "RECOVERY_REQUIRED"
                    or (source_entry["status"] == "REVERTED" and not finalization)):
                raise ValueError("PRIOR_TRANSACTION_RECOVERY_REQUIRED")
            source_result = source_entry["result"]
            envelope = dict(tx_hash=saved["tx_hash"], raw_transaction=saved["raw_transaction"], nonce=str(nonce))
            if any(source_result[field] != value for field, value in envelope.items() if field in source_result):
                if source_result.get("block_hash") or not is_related(identity_journal, saved, source_result):
                    raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
                source_result = {field: saved[field] for field in SIGNED_KEYS if field in saved}
                source_journal.update(saved["operation"], "SIGNED", source_result)
                source_entry = source_journal.get(saved["operation"])
            suffix = ":" + source_entry["request"]["id"]
            if not saved["operation"].endswith(suffix):
                raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
            prefix = saved["operation"][:-len(suffix)]
            binding = (str(source), prefix)
            if source_entry["status"] != "REVERTED" and binding not in checked_sources:
                verify_source_anchors(bootstrap.w3, source_journal, prefix)
                checked_sources.add(binding)
            outcome = replacement_status(bootstrap, identity_journal, account, saved, source_journal, source_entry,
                getattr(bootstrap, "evm_confirmations", 1)) if row["status"] == TRACKING else None
            if outcome and outcome["status"] == "CANCELLED":
                floor = max(floor, nonce + 1)
                continue
            if outcome and outcome["status"] == "BROADCAST":
                if replacements is not None:
                    replacements[(str(source), saved["operation"])] = outcome
                replacement_floor = max(replacement_floor, nonce + 1)
                continue
            if outcome and outcome["status"] == "WINNER":
                saved = outcome["saved"]
                key = saved["tx_hash"]
                source_entry = source_journal.get(saved["operation"])
                source_result = source_entry["result"]
                if replacements is not None:
                    replacements[(str(source), saved["operation"])] = dict(status="WINNER", result=source_result)
        try:
            receipt = bootstrap.w3.eth.get_transaction_receipt(saved["tx_hash"])
        except TransactionNotFound:
            if source_result.get("block_hash"):
                verify_anchor(bootstrap.w3, int(source_result["block_number"]), source_result["block_hash"])
                raise ValueError("EVM_RPC_BEHIND") from None
            if nonce < floor:
                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
            if source_entry is not None and source_entry["status"] == "REVERTED":
                raise ValueError("PRIOR_TRANSACTION_RECOVERY_REQUIRED")
            if (replacements is not None and source_entry is not None and chain_id in (1, 11155111)
                    and tx.get("maxFeePerGas") is not None and not source_entry["request"].get("create_group")
                    and not source_entry["request"].get("parallel_group")):
                head = bootstrap.w3.eth.get_block("latest")
                base = getattr(head, "baseFeePerGas", None)
                if base is not None and tx["maxFeePerGas"] < base:
                    if account is not None and bootstrap.w3.eth.get_transaction_count(account.address, head.number) > nonce:
                        raise ValueError("EVM_REPLACEMENT_NONCE_CONFLICT")
                    replacements[(str(source), saved["operation"])] = dict(status="BROADCAST",
                        tx_hash=saved["tx_hash"], reason="EVM_PENDING_FEE_CAP_BELOW_BASE")
                    replacement_floor = max(replacement_floor, nonce + 1)
                    continue
            try:
                bootstrap.w3.eth.get_transaction(saved["tx_hash"])
            except TransactionNotFound:
                if source_entry is not None:
                    step = source_entry["request"]
                    if is_reservation_transfer(prefix, step):
                        validate_rebroadcast(bootstrap.w3, source_journal, prefix, step, tx,
                                             lineage_sender(saved, account),
                                             confirmation_floor=getattr(bootstrap, "evm_confirmations", 1))
                    require_publication_context(bootstrap, source_journal, prefix, step)
                bootstrap.broadcast(saved["raw_transaction"])
        else:
            evidence = dict(source_result, tx_hash=saved["tx_hash"])
            if receipt.status == 1 and source_entry is not None and source_entry["status"] == "CONFIRMED":
                if bootstrap.verify_receipt(evidence, receipt=receipt) is not None:
                    identity_journal.update(key, "CONFIRMED", dict(block_number=str(receipt.blockNumber),
                        block_hash=receipt.blockHash.hex()))
                continue
            check_receipt_binding(receipt, evidence)
            verify_anchor(bootstrap.w3, receipt.blockNumber, receipt.blockHash)
            if receipt.status != 1:
                if source_entry is not None and source_entry["status"] != "REVERTED":
                    source_journal.update(saved["operation"], "REVERTED", dict(
                        source_result, block_number=str(receipt.blockNumber), block_hash=receipt.blockHash.hex()))
                if finalization and reconcile(bootstrap.w3, identity_journal, account, saved, source_journal,
                                              source_entry["request"], receipt):
                    floor = max(floor, nonce + 1)
                    continue
                raise ValueError("PRIOR_TRANSACTION_RECOVERY_REQUIRED")
            if source_entry is not None and source_entry["status"] == "REVERTED":
                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    return max(floor, replacement_floor)


def verify_source_anchors(w3, journal, prefix, *, allow_recovery=False, confirmation_floor=1):
    """Do not replay a dependent signed transaction from an orphaned read view.

    Identity recovery may be invoked by a different deployment/profile. It must
    validate the saved source's observation anchors before it can bypass that
    source's normal sequential resume path. Conditions are never re-evaluated.
    """
    from .evm_network import read_block, verify_anchor
    leading = prefix + ":"
    with journal.db() as db:
        rows = db.execute("SELECT status,result FROM operations WHERE substr(id,1,?)=?",
                          (len(leading), leading)).fetchall()
    # A legacy observation may have failed conservative migration without ever
    # gaining an anchor. Respect that recorded stop before replaying a later
    # signed transaction from another caller/journal.
    if not allow_recovery and any(row["status"] in ("REVERTED", "RECOVERY_REQUIRED") for row in rows):
        raise ValueError("PRIOR_TRANSACTION_RECOVERY_REQUIRED")
    anchors = []
    for row in rows:
        result = json.loads(row["result"])
        if result.get("read_anchor") is not None:
            anchors.append(result["read_anchor"])
        if row["status"] == "CONFIRMED" and "block_hash" in result:
            anchors.append(dict(number=result["block_number"], hash=result["block_hash"]))
    if not anchors:
        return  # Historical journals did not record such anchors.
    profile = journal.get(prefix)["request"]["profile"]
    safe = read_block(w3, max(confirmation_floor, int(profile.get("evm_confirmations", "1"))))
    blocks = {safe.number: safe}
    for anchor in anchors:
        number = int(anchor["number"])
        if number not in blocks:
            blocks[number] = verify_anchor(w3, number, anchor["hash"], safe=safe)
        if HexBytes(blocks[number].hash) != HexBytes(anchor["hash"]):
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")


def validate_targets(bootstrap, steps):
    """Same-target calls are forbidden except independent DKG session creation."""
    targets = {}
    for step in steps:
        if step["kind"] != "call":
            continue
        item = bootstrap.contracts[step["contract"]]
        target = Web3.to_checksum_address(item["address"])
        targets.setdefault(target, []).append(step)
    for target, calls in targets.items():
        if len(calls) < 2:
            continue
        sessions = set()
        for step in calls:
            item = bootstrap.contracts[step["contract"]]
            args = bootstrap.resolve(step.get("args", []))
            if item["artifact"] != "DKGCoordinator" or step["method"] != "createCeremony" or len(args) != 7:
                raise ValueError("BOOTSTRAP_PARALLEL_TARGET")
            session = args[0]
            if (not isinstance(session, str) or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", session)
                    or int(session.removeprefix("0x"), 16) == 0
                    or session.removeprefix("0x").lower() in sessions):
                raise ValueError("BOOTSTRAP_PARALLEL_SESSION")
            sessions.add(session.removeprefix("0x").lower())
        from .bitcoin_trust import checked_runtime
        number = bootstrap.read_number()
        block = bootstrap.w3.eth.get_block(number)
        checked_runtime(bootstrap.w3, target, "DKGCoordinator", block.number)
        if bootstrap.w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")


def observe(bootstrap, step, entry):
    """Return a finalized receipt, or None; save mined anchors before returning."""
    from .bootstrap_recovery import check_receipt_binding
    from .evm_network import verify_anchor

    key, result = entry["id"], entry["result"]
    if entry["status"] in ("REVERTED", "RECOVERY_REQUIRED"):
        raise ValueError("PRIOR_TRANSACTION_RECOVERY_REQUIRED")
    if "tx_hash" not in result:
        return None
    try:
        receipt = bootstrap.w3.eth.get_transaction_receipt(result["tx_hash"])
    except TransactionNotFound:
        if result.get("block_hash"):
            verify_anchor(bootstrap.w3, int(result["block_number"]), result["block_hash"])
        if result.get("block_hash") or entry["status"] == "CONFIRMED":
            raise ValueError("EVM_RPC_BEHIND") from None
        return None
    anchor = dict(block_number=str(receipt.blockNumber), block_hash=receipt.blockHash.hex())
    if receipt.status == 0:
        check_receipt_binding(receipt, result)
        verify_anchor(bootstrap.w3, receipt.blockNumber, receipt.blockHash)
        result.update(anchor)
        bootstrap.journal.update(key, "REVERTED", result)
        raise ValueError("TRANSACTION_REVERTED")
    confirmed = bootstrap.verify_receipt(result, receipt=receipt)
    if entry["status"] == "CONFIRMED" and confirmed is None:
        raise ValueError("EVM_RPC_BEHIND")
    changed = entry["status"] != "CONFIRMING" or any(result.get(k) != v for k, v in anchor.items())
    result.update(anchor)
    if entry["status"] != "CONFIRMED" and changed:
        bootstrap.journal.update(key, "CONFIRMING", result)
        entry["status"] = "CONFIRMING"
    return confirmed


def finish_group(bootstrap, steps, entries, receipts, identity_journal=None):
    from .transaction_wait import observation_pending
    # Install every finalized deployment output before checking group-wide
    # postconditions. Nothing outside the group executes until all checks pass.
    for step, entry, receipt in zip(steps, entries, receipts):
        bootstrap.record_deployment(step, entry["result"], receipt)
        bootstrap.outputs(step, entry["result"])
    for step, entry, receipt in zip(steps, entries, receipts):
        try:
            bootstrap.verify_checks(step, receipt)
        except Exception as error:
            if observation_pending(error):
                return dict(status="CONFIRMING", step=step["id"], reason="RPC_OBSERVATION_PENDING",
                            tx_hash=entry["result"]["tx_hash"])
            bootstrap.journal.update(entry["id"], "RECOVERY_REQUIRED", entry["result"])
            raise
    # Reads/postconditions may span RPC requests. Recheck every receipt before
    # opening the barrier, including finalized-head regression or a reorg.
    for step, entry in zip(steps, entries):
        if observe(bootstrap, step, entry) is None:
            raise ValueError("EVM_RPC_BEHIND")
    for entry in entries:
        bootstrap.journal.update(entry["id"], "CONFIRMED", entry["result"])
        if identity_journal is not None:
            identity_journal.update(entry["result"]["tx_hash"], "CONFIRMED",
                {key: entry["result"][key] for key in ("block_hash", "block_number")})


def apply_group(bootstrap, steps, account, identity, *, dry_run=False, verify=False, before_submit=None):
    import fcntl
    from .bootstrap_recovery import signed_nonce
    entries = [bootstrap.journal.put(bootstrap.prefix + ":" + step["id"], step) for step in steps]
    receipts = [observe(bootstrap, step, entry) for step, entry in zip(steps, entries)]
    if all(receipts):
        return finish_group(bootstrap, steps, entries, receipts)
    first = next(step for step, receipt in zip(steps, receipts) if receipt is None)
    if verify:
        return dict(status="INCOMPLETE", step=first["id"])
    if dry_run:
        return dict(status="DRY_RUN", next_step=first, manifest=bootstrap.manifest())
    if account is None:
        raise ValueError("LOCAL_EVM_IDENTITY_REQUIRED")
    recover_only = bool(before_submit(steps)) if before_submit is not None else False
    validate_targets(bootstrap, steps)
    lock = Path(identity).resolve().with_suffix(".evm-nonce.lock")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        identity_journal = Journal(str(lock) + ".sqlite")
        # Restore a global reservation even if the previous process died before
        # it could write the signed bytes to its step journal.
        for step, entry in zip(steps, entries):
            saved = restore_signed(identity_journal, bootstrap, entry["id"], step, entry["result"])
            if saved is not None and "raw_transaction" not in entry["result"]:
                entry["result"] = saved
                bootstrap.journal.update(entry["id"], "SIGNED", saved)
            if "raw_transaction" in entry["result"]:
                signed_nonce(entry["result"], account, bootstrap.w3.eth.chain_id)
        if recover_only and any("raw_transaction" not in entry["result"] for entry in entries):
            return dict(status="NOT_SIGNED", step=first["id"], parallel_group=steps[0]["parallel_group"])
        replacements = {}
        reservations = set()
        floor = recover_identity(identity_journal, bootstrap, account, replacements=replacements, reservations=reservations)
        nonce = bootstrap.w3.eth.get_transaction_count(account.address, "pending")
        if any("raw_transaction" not in entry["result"] for entry in entries) and nonce < max(floor, max(reservations, default=-1) + 1):
            waiting = next((row for row in replacements.values() if row["status"] == "BROADCAST"), None)
            if waiting is not None:
                return dict(step=first["id"], parallel_group=steps[0]["parallel_group"], **waiting)
            raise ValueError("EVM_RPC_BEHIND")
        prepared = {}
        # Resolve and estimate ALL independent operations before the first new
        # signature/send. An invalid late constructor cannot spend a prefix.
        for step, entry in zip(steps, entries):
            if "raw_transaction" not in entry["result"]:
                prepared[entry["id"]] = bootstrap.build_transaction(step, account, nonce)
                nonce += 1
        for index, (step, entry) in enumerate(zip(steps, entries)):
            result = entry["result"]
            needs_signature = "raw_transaction" not in result
            if not needs_signature:
                try:
                    identity_journal.get(result["tx_hash"])
                except KeyError:
                    save_signed(identity_journal, bootstrap, entry["id"], step, result)
                try:
                    bootstrap.w3.eth.get_transaction(result["tx_hash"])
                except TransactionNotFound:
                    pass
                else:
                    continue
            # A new send requires fresh observations of earlier transactions.
            # Known transactions still get their final group-wide observation.
            for prior_step, prior_entry in zip(steps[:index], entries[:index]):
                observe(bootstrap, prior_step, prior_entry)
            if needs_signature:
                transaction = prepared[entry["id"]]
                raw = bytes(account.sign_transaction(transaction).raw_transaction)
                result.update(tx_hash=Web3.keccak(raw).hex(), raw_transaction=raw.hex(), nonce=str(transaction["nonce"]))
                save_signed(identity_journal, bootstrap, entry["id"], step, result)
            # The signed envelope itself is checked before recovery/broadcast;
            # neither a changed local nonce nor a new estimate can replace it.
            signed_at = signed_nonce(result, account, bootstrap.w3.eth.chain_id)
            if signed_at < floor:
                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
            bootstrap.broadcast(result["raw_transaction"])
            bootstrap.journal.update(entry["id"], "BROADCAST", result)
        receipts = [observe(bootstrap, step, entry) for step, entry in zip(steps, entries)]
        if all(receipts):
            return finish_group(bootstrap, steps, entries, receipts, identity_journal)
    return dict(status="CONFIRMING" if all(entry["result"].get("block_hash") for entry in entries) else "BROADCAST",
                step=first["id"], parallel_group=steps[0]["parallel_group"],
                pending=[step["id"] for step, receipt in zip(steps, receipts) if receipt is None])

"""Review and transfer an unused organization reservation to a new DKG session."""

import hashlib
import re
from types import SimpleNamespace

from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound
from web3.logs import DISCARD

from .bitcoin_trust import checked_runtime
from .bootstrap import Bootstrap, artifact, artifact_fingerprint
from .dao_client import account_at
from .evm_network import read_block, verify_anchor
from .evm_rpc import evm_web3
from .operations import Journal, canonical
from .transaction_wait import apply_confirmed


ZERO_ADDRESS = "0x" + "00" * 20
EVM_AUTH = Web3.keccak(text="EVM_AUTH")
PHASES = ("NotStarted", "Commitments", "Shares", "PublicKey", "Completed", "Failed", "Aborted")
PREFIX = "reservation-transfer-"


class ReservationExecutor(Bootstrap):
    def resolve(self, value):
        # Reservation IDs are literal contract strings, including a leading "$".
        return value


def add_parser(sub):
    parser = sub.add_parser("reservation-transfer", help="review or send an exact unused reservation transfer")
    parser.add_argument("--kind", choices=("federation", "confederation"), required=True)
    parser.add_argument("--factory", required=True)
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--from-session", required=True)
    parser.add_argument("--to-session", required=True)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--chain-id", choices=("1", "11155111", "31337"), required=True)
    parser.add_argument("--confirmations", type=int, required=True)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--accept-fingerprint")
    parser.add_argument("--identity-file")
    parser.add_argument("--state")


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _session(value):
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", value):
        raise ValueError("RESERVATION_TRANSFER_INPUT")
    raw = bytes.fromhex(value.removeprefix("0x"))
    if not any(raw):
        raise ValueError("RESERVATION_TRANSFER_INPUT")
    return raw


def _contract(w3, address, kind, block):
    if not Web3.is_checksum_address(address) or address == ZERO_ADDRESS:
        raise ValueError("RESERVATION_TRANSFER_BINDING")
    runtime = checked_runtime(w3, address, kind, block)
    return w3.eth.contract(address=address, abi=artifact(kind)["abi"]), runtime


def _read(node, block, method, *args):
    return getattr(node.functions, method)(*args).call(block_identifier=block.number)


def _factory(w3, inputs, block):
    kind = inputs["kind"].capitalize() + "Factory"
    node, factory_runtime = _contract(w3, inputs["factory"], kind, block.number)
    operator = _read(node, block, "feeSetupAuthority")
    if not Web3.is_checksum_address(operator) or operator == ZERO_ADDRESS:
        raise ValueError("RESERVATION_TRANSFER_BINDING")
    registry, registry_runtime = _contract(w3, _read(node, block, "dkgCoordinator"), "DKGCoordinator", block.number)
    coordinator, coordinator_runtime = _contract(w3, _read(node, block, "frostCoordinator"),
                                                 "FROSTCoordinator", block.number)
    if _read(coordinator, block, "dkgRegistry") != registry.address:
        raise ValueError("RESERVATION_TRANSFER_BINDING")
    return node, registry, coordinator, operator, dict(factory=factory_runtime, registry=registry_runtime,
                                                        coordinator=coordinator_runtime)


def require_live_replacement(target, timestamp):
    if (target[4] not in (1, 2, 3, 4) or target[9] != (target[4] == 4)
            or (not target[9] and timestamp > target[5])):
        raise ValueError("RESERVATION_TRANSFER_REPLACEMENT_UNAVAILABLE")


def _inspect(w3, inputs, previous, replacement, block):
    title = inputs["kind"].capitalize()
    factory, registry, coordinator, operator, runtimes = _factory(w3, inputs, block)
    entity = inputs["entity_id"]
    if bytes(_read(factory, block, "reserved" + title + "Session", entity)) != previous:
        raise ValueError("RESERVATION_TRANSFER_CHANGED")
    formation, token = _read(factory, block, "reserved" + title + "Terms", entity)
    if not any(formation) or not any(token):
        raise ValueError("RESERVATION_TRANSFER_BINDING")

    source = _read(registry, block, "getSessionInfo", previous)
    target = _read(registry, block, "getSessionInfo", replacement)
    if source[4] not in (5, 6) or source[9]:
        raise ValueError("RESERVATION_TRANSFER_SOURCE_NOT_TERMINAL")
    require_live_replacement(target, block.timestamp)
    source_ceremony = _read(registry, block, "ceremonies", previous)
    target_ceremony = _read(registry, block, "ceremonies", replacement)
    roster = list(_read(registry, block, "getSessionParticipants", previous))
    if (source_ceremony[2] != EVM_AUTH or target_ceremony[2] != EVM_AUTH
            or not any(source_ceremony[0]) or not any(target_ceremony[0])
            or source[3] != target[3] or roster != list(_read(registry, block, "getSessionParticipants", replacement))):
        raise ValueError("RESERVATION_TRANSFER_DKG_MISMATCH")
    method = "transfer" + title + "Reservation"
    call = getattr(factory.functions, method)(entity, previous, replacement)
    try:
        _read(registry, block, "requireReservationTransfer", previous, replacement)
        call.call({"from": operator}, block_identifier=block.number)
    except ContractLogicError as exc:
        raise ValueError("RESERVATION_TRANSFER_SIMULATION_REJECTED") from exc
    verify_anchor(w3, block.number, block.hash)
    review = dict(
        **inputs, domain="SATOHSI_RESERVATION_TRANSFER_V1", operator=operator,
        registry=registry.address, coordinator=coordinator.address, runtime_hashes=runtimes,
        genesis_hash=Web3.to_hex(w3.eth.get_block(0).hash),
        factory_build=artifact_fingerprint(artifact(title + "Factory")),
        registry_build=artifact_fingerprint(artifact("DKGCoordinator")),
        coordinator_build=artifact_fingerprint(artifact("FROSTCoordinator")),
        source_status=PHASES[source[4]], source_context=Web3.to_hex(source_ceremony[0]),
        replacement_context=Web3.to_hex(target_ceremony[0]), members=roster, threshold=str(source[3]),
        formation_hash=Web3.to_hex(formation), token_hash=Web3.to_hex(token), method=method,
        calldata=factory.encode_abi(method, args=[entity, previous, replacement]),
    )
    return review, PHASES[target[4]]


def _profile(args, prefix, review, confirmations):
    title = review["kind"].capitalize()
    entity = review["entity_id"]
    checks = [
        dict(contract="factory", method="reserved" + title + "Session", args=[entity], equals=review["to_session"],
             at_receipt=True),
        dict(contract="factory", method="reserved" + title + "Terms", args=[entity],
             equals=[review["formation_hash"], review["token_hash"]], at_receipt=True),
    ]
    step = dict(id="transfer", kind="call", contract="factory", method=review["method"],
                args=[entity, review["from_session"], review["to_session"]], checks=checks)
    mainnet = review["chain_id"] == "1"
    return dict(
        deployment_id=prefix, evm_rpc=args.rpc, chain_id=review["chain_id"],
        evm_confirmations=str(confirmations), bitcoin_network="mainnet" if mainnet else "regtest",
        mode="backed" if mainnet else "integration",
        contracts=dict(factory=dict(address=review["factory"], artifact=title + "Factory",
                                    artifact_hash=review["factory_build"],
                                    runtime_hash=review["runtime_hashes"]["factory"])),
        steps=[step],
    )


def _exact_receipt(w3, review, entry, *, expected=None):
    # Observing the desired session in storage is insufficient. Recover the
    # exact journaled transaction, including its original calldata and sender.
    tx_result = entry["result"] if entry is not None else {}
    if not tx_result.get("tx_hash") or not tx_result.get("raw_transaction"):
        raise ValueError("RESERVATION_TRANSFER_CHANGED")
    from .bootstrap_recovery import check_receipt_binding, signed_fields

    signer = SimpleNamespace(address=review["operator"])
    fields = signed_fields(tx_result, signer, w3.eth.chain_id)
    if (fields["to"] != HexBytes(review["factory"])
            or fields["data"] != HexBytes(review["calldata"]) or fields["value"] != 0):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    try:
        receipt = expected or w3.eth.get_transaction_receipt(tx_result["tx_hash"])
    except TransactionNotFound:
        raise ValueError("RESERVATION_TRANSFER_CHANGED") from None
    check_receipt_binding(receipt, tx_result)
    if receipt.status != 1:
        raise ValueError("RESERVATION_TRANSFER_RECEIPT")
    verify_anchor(w3, receipt.blockNumber, receipt.blockHash)
    return receipt


def _verify_event(w3, review, receipt):
    title = review["kind"].capitalize()
    block = verify_anchor(w3, receipt.blockNumber, receipt.blockHash)
    factory, runtime = _contract(w3, review["factory"], title + "Factory", block.number)
    if runtime != review["runtime_hashes"]["factory"]:
        raise ValueError("RESERVATION_TRANSFER_RECEIPT")
    events = getattr(factory.events, title + "ReservationTransferred")().process_receipt(receipt, errors=DISCARD)
    if (len(events) != 1 or events[0]["address"] != review["factory"]
            or bytes(events[0]["args"][review["kind"] + "Id"]) != Web3.keccak(text=review["entity_id"])
            or Web3.to_hex(events[0]["args"]["previous"]) != review["from_session"]
            or Web3.to_hex(events[0]["args"]["replacement"]) != review["to_session"]):
        raise ValueError("RESERVATION_TRANSFER_RECEIPT")
    verify_anchor(w3, block.number, block.hash)


def _entry(journal, key):
    try:
        return journal.get(key)
    except KeyError:
        return None


def is_reservation_transfer(prefix, step):
    if not prefix.startswith(PREFIX):
        return False
    if (not re.fullmatch(r"reservation-transfer-[0-9a-f]{64}", prefix)
            or step.get("id") != "transfer" or step.get("kind") != "call" or step.get("contract") != "factory"
            or step.get("method") not in ("transferFederationReservation", "transferConfederationReservation")):
        raise ValueError("RESERVATION_TRANSFER_BINDING")
    return True


def bound_review(w3, journal, prefix, step, fields, sender):
    """Bind the signed call to its saved operator review, even during cancellation."""
    try:
        memo = journal.get(prefix + ":review")["request"]
        review = memo["review"]
        inputs = {key: review[key] for key in
                  ("chain_id", "kind", "entity_id", "factory", "from_session", "to_session")}
        if (not is_reservation_transfer(prefix, step) or prefix != PREFIX + _digest(inputs)
                or review["chain_id"] != str(w3.eth.chain_id)
                or sender != review["operator"]
                or HexBytes(review["genesis_hash"]) != w3.eth.get_block(0).hash
                or step["method"] != review["method"]
                or step["args"] != [review["entity_id"], review["from_session"], review["to_session"]]
                or HexBytes(fields["to"]) != HexBytes(review["factory"])
                or HexBytes(fields["data"]) != HexBytes(review["calldata"]) or fields["value"] != 0):
            raise ValueError("RESERVATION_TRANSFER_BINDING")
    except (KeyError, TypeError):
        raise ValueError("RESERVATION_TRANSFER_BINDING") from None
    return memo, inputs


def validate_rebroadcast(w3, journal, prefix, step, fields, sender, *, confirmation_floor=1):
    """Shared nonce recovery can relay an old transfer only while its review still holds."""
    memo, inputs = bound_review(w3, journal, prefix, step, fields, sender)
    safe = read_block(w3, max(confirmation_floor, int(memo["confirmations"])))
    verify_anchor(w3, int(memo["number"]), memo["hash"], safe=safe)
    latest = w3.eth.get_block("latest")
    verify_anchor(w3, int(memo["number"]), memo["hash"], safe=latest)
    now, _ = _inspect(w3, inputs, _session(inputs["from_session"]), _session(inputs["to_session"]), latest)
    if now != memo["review"]:
        raise ValueError("RESERVATION_TRANSFER_FINGERPRINT")


def cancelled_result(w3, entry, safe):
    result = entry["result"]
    verify_anchor(w3, int(result["block_number"]), result["block_hash"], safe=safe)
    return dict(status="RESERVATION_TRANSFER_CANCELLED",
                original_tx_hash=result["original_tx_hash"], tx_hash=result["cancellation_tx_hash"])


def execute(args):
    previous, replacement = _session(args.from_session), _session(args.to_session)
    if (previous == replacement or not args.entity_id or not Web3.is_address(args.factory)
            or int(args.factory, 16) == 0):
        raise ValueError("RESERVATION_TRANSFER_INPUT")
    if args.send and (not args.identity_file or not args.state or not args.accept_fingerprint):
        raise ValueError("RESERVATION_TRANSFER_SEND_OPTIONS")
    w3 = evm_web3(args.rpc, timeout=20)
    if str(w3.eth.chain_id) != args.chain_id:
        raise ValueError("CHAIN_MISMATCH")
    block = read_block(w3, args.confirmations)
    inputs = dict(chain_id=args.chain_id, kind=args.kind, entity_id=args.entity_id,
                  factory=Web3.to_checksum_address(args.factory),
                  from_session=Web3.to_hex(previous), to_session=Web3.to_hex(replacement))
    if not args.send:
        review, phase = _inspect(w3, inputs, previous, replacement, block)
        return dict(status="RESERVATION_TRANSFER_REVIEW", review=review, fingerprint=_digest(review),
                    replacement_status=phase, block_number=str(block.number), block_hash=Web3.to_hex(block.hash))

    prefix = PREFIX + _digest(inputs)
    journal = Journal(args.state)
    memo_key = prefix + ":review"
    saved = _entry(journal, memo_key)
    if saved is None:
        review, _ = _inspect(w3, inputs, previous, replacement, block)
        memo = dict(review=review, number=str(block.number), hash=Web3.to_hex(block.hash),
                    confirmations=str(args.confirmations))
    else:
        memo = saved["request"]
        review = memo["review"]
    fingerprint = _digest(review)
    if fingerprint != args.accept_fingerprint or any(review.get(key) != value for key, value in inputs.items()):
        raise ValueError("RESERVATION_TRANSFER_FINGERPRINT")
    if account_at(args.identity_file).address != review["operator"]:
        raise ValueError("SENDER_IDENTITY_MISMATCH")
    confirmations = max(int(memo["confirmations"]), args.confirmations)
    public = dict(fingerprint=fingerprint, factory=review["factory"], entity_id=review["entity_id"])
    try:
        safe = block if confirmations == args.confirmations else read_block(w3, confirmations)
        entry = _entry(journal, prefix + ":transfer")
        if entry is not None and entry["status"] == "CANCELLED":
            return dict(public, **cancelled_result(w3, entry, safe))
        verify_anchor(w3, int(memo["number"]), memo["hash"], safe=safe)

        def gate(provider):
            latest = provider.eth.get_block("latest")
            verify_anchor(provider, int(memo["number"]), memo["hash"], safe=latest)
            try:
                now, _ = _inspect(provider, inputs, previous, replacement, latest)
            except ValueError as exc:
                if str(exc) != "RESERVATION_TRANSFER_CHANGED":
                    raise
                _exact_receipt(provider, review, _entry(journal, prefix + ":transfer"))
                return True  # Recover only the exact already-mined journaled transaction.
            if _digest(now) != fingerprint:
                raise ValueError("RESERVATION_TRANSFER_FINGERPRINT")
            return False

        if saved is None:
            journal.put(memo_key, memo)
        profile = _profile(args, prefix, review, int(memo["confirmations"]))
        archived_profile = _entry(journal, prefix)
        if archived_profile is not None:
            archived = archived_profile["request"]["profile"]
            if dict(archived, evm_rpc=args.rpc) != profile:
                raise ValueError("RESERVATION_TRANSFER_FINGERPRINT")
            profile = archived
        runner = ReservationExecutor(profile, args.state, evm_rpc_override=args.rpc, evm_confirmation_floor=confirmations)
        def apply(identity):
            return runner.apply(identity, before_submit=lambda _steps: gate(runner.w3))

        result = apply_confirmed(SimpleNamespace(apply=apply), args.identity_file)
        if result["status"] == "RESERVATION_TRANSFER_CANCELLED":
            return dict(public, **result)
        entry = _entry(journal, prefix + ":transfer")
        tx_hash = entry["result"].get("tx_hash") if entry is not None else None
        payload = dict(public, status=result["status"], tx_hash=tx_hash)
        if "reason" in result:
            payload["reason"] = result["reason"]
        if result["status"] == "VERIFIED":
            receipt = runner.verify_receipt(entry["result"])
            if receipt is None:
                return dict(payload, status="CONFIRMING")
            _exact_receipt(runner.w3, review, entry, expected=receipt)
            _verify_event(runner.w3, review, receipt)
            return dict(payload, status="RESERVATION_TRANSFERRED")
        return payload
    except ValueError as exc:
        if str(exc) == "EVM_RPC_BEHIND":
            return dict(public, status="AWAITING_EVM_RPC", reason="EVM_RPC_BEHIND")
        raise

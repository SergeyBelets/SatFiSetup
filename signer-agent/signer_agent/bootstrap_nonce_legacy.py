"""Bind a legacy signer nonce reservation to its existing FROST operation."""

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace

from eth_abi.exceptions import DecodingError
from hexbytes import HexBytes
from web3 import Web3
from .evm_rpc import evm_web3
from web3.exceptions import MismatchedABI

from .bitcoin_trust import runtime_artifact
from .bootstrap import Bootstrap, artifact, encode_args
from .bootstrap_parallel import operation_binding
from .bootstrap_recovery import anchor, identity_lane, signed_fields
from .operations import Journal, canonical, decode_entry


LEGACY_FIELDS = {"chain_id", "tx_hash", "raw_transaction"}
FROST_METHODS = {
    "submitNonceCommitments", "submitVerifiableSignatureShare",
    "rejectSignatureRequest", "finalizeSession",
}
SOURCE_STATES = {"SIGNED", "BROADCAST", "CONFIRMING", "CONFIRMED", "REVERTED"}
BOUND = "EVM_LEGACY_NONCE_BOUND"


@contextmanager
def _locked(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("JOURNAL_FILE_PERMISSIONS")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _checked_entry(entry):
    if entry["digest"] != hashlib.sha256(canonical(entry["request"]).encode()).hexdigest():
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    return entry


def _prove_source(w3, journal, saved, fields):
    if (fields.get("type", 0) not in (0, 2) or len(fields["to"]) != 20
            or fields["value"] != 0 or not fields["data"]):
        raise ValueError("EVM_LEGACY_NONCE_UNSUPPORTED")
    target = Web3.to_checksum_address(fields["to"])
    coordinator = w3.eth.contract(address=target, abi=artifact("FROSTCoordinator")["abi"])
    try:
        decoded, args = coordinator.decode_function_input(fields["data"])
    except (ValueError, MismatchedABI, DecodingError):
        raise ValueError("EVM_LEGACY_NONCE_UNSUPPORTED") from None
    method = decoded.fn_name
    if method not in FROST_METHODS:
        raise ValueError("EVM_LEGACY_NONCE_UNSUPPORTED")
    values = Bootstrap.wire([args[item["name"]] for item in decoded.abi["inputs"]])
    deployment = "signer-" + Web3.keccak(text=canonical([target, method, values])).hex()
    operation = deployment + ":send"
    step = dict(id="send", kind="call", contract="target", method=method, args=values)
    try:
        source = _checked_entry(journal.get(operation))
        profile = _checked_entry(journal.get(deployment))["request"]["profile"]
        contracts = profile["contracts"]
        spec = contracts["target"]
    except KeyError:
        raise ValueError("EVM_LEGACY_NONCE_SOURCE_REQUIRED") from None
    nonce = str(fields["nonce"])
    if (source["request"] != step or source["status"] not in SOURCE_STATES
            or profile.get("deployment_id") != deployment or profile.get("chain_id") != saved["chain_id"]
            or profile.get("steps") != [step] or set(contracts) != {"target"}
            or spec.get("artifact") != "FROSTCoordinator" or spec.get("address") != target
            or any(source["result"].get(key) != value for key, value in
                   dict(tx_hash=saved["tx_hash"], raw_transaction=saved["raw_transaction"], nonce=nonce).items())):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")

    function = getattr(coordinator.functions, method)(*encode_args(decoded.abi["inputs"], source["request"]["args"]))
    if bytes(HexBytes(function._encode_transaction_data())) != bytes(fields["data"]):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    block = anchor(w3)
    runtime, _ = runtime_artifact(w3, target, "FROSTCoordinator", block.number)
    if not spec.get("runtime_hash") or HexBytes(spec["runtime_hash"]) != HexBytes(runtime):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    return operation_binding(SimpleNamespace(journal=journal), operation, step)


def bind_legacy_nonce(identity, source_journal, rpc, tx_hash):
    """Enrich only the private lane. The source, signed bytes and receipt state stay intact."""
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", tx_hash or ""):
        raise ValueError("EVM_LEGACY_NONCE_INPUTS_REQUIRED")
    tx_hash = HexBytes(tx_hash).hex()
    account, nonce_lock, lane = identity_lane(identity, require_existing=True)
    path = Path(source_journal)
    if path.is_symlink() or not path.is_file() or path.samefile(lane.path):
        raise ValueError("EVM_LEGACY_NONCE_SOURCE_REQUIRED")
    journal = Journal(path.resolve())
    source_lock = Path(str(journal.path) + ".lock")
    if source_lock == nonce_lock:
        raise ValueError("EVM_LEGACY_NONCE_SOURCE_REQUIRED")

    # Bootstrap and replacement both acquire the operation lock first.
    with _locked(source_lock), _locked(nonce_lock):
        try:
            before = _checked_entry(lane.get(tx_hash))
        except KeyError:
            raise ValueError("EVM_REPLACEMENT_NOT_JOURNALED") from None
        saved = before["request"]
        legacy = set(saved) == LEGACY_FIELDS
        if saved.get("tx_hash") != tx_hash or "replacement_root" in saved:
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        w3 = evm_web3(rpc, timeout=20)
        chain = w3.eth.chain_id
        if chain not in (1, 31337, 11155111) or saved.get("chain_id") != str(chain):
            raise ValueError("CHAIN_MISMATCH")
        fields = signed_fields(saved, account, chain)
        nonce = str(fields["nonce"])
        binding = _prove_source(w3, journal, saved, fields)
        expected = dict(nonce=nonce, **binding)
        if legacy:
            if before["status"] not in ("PREPARED", "CONFIRMED"):
                raise ValueError("EVM_LEGACY_NONCE_UNSUPPORTED")
            _append_binding(lane, before, expected)
        elif any(saved.get(key) != value for key, value in expected.items()):
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        return dict(status=BOUND, tx_hash=tx_hash, nonce=nonce, operation=binding["operation"])


def _append_binding(lane, before, binding):
    request = canonical(dict(before["request"], **binding))
    digest = hashlib.sha256(request.encode()).hexdigest()
    with lane.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM operations WHERE id=?", (before["id"],)).fetchone()
        if row is None or decode_entry(row) != before:
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        db.execute("UPDATE operations SET request=?,digest=? WHERE id=?", (request, digest, before["id"]))
        db.execute("INSERT INTO events(operation,status) VALUES(?,?)", (before["id"], BOUND))

"""Export locally produced ZK evidence and review it without adopting a proof."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

from web3 import Web3
from web3.exceptions import ContractLogicError

from .bootstrap import artifact
from .bootstrap_policy import validate_confirmations
from .evm_network import read_block, verify_anchor
from .operations import canonical
from .zk_spv import VERIFIER_SELECTOR, VERIFIER_VERSION, checkpoint_descriptor, inspect_binding
from .zk_spv_cli import validate_transition_journal
from .zk_worker_prover import LocalProver


SCHEMA = "satoshifi.zk-peer-bundle.v1"
MANIFEST = "manifest.json"
JOURNAL = "state.journal"
SEAL = "groth16.seal"
JOURNAL_SIZE = 1216
SEAL_SIZE = 260
MANIFEST_LIMIT = 4096
BUNDLE_LIMIT = 8192


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _attempt_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("ZK_PEER_BUNDLE_ATTEMPT")
    return value


def _scope(config):
    chain_id = config["chain_id"]
    state = config["zk_state"]
    if chain_id not in ("1", "11155111", "31337") or not Web3.is_checksum_address(state) or int(state, 16) == 0:
        raise ValueError("ZK_PEER_BUNDLE_SCOPE")
    validate_confirmations(config, "ZK_PEER_BUNDLE_")
    policy = config["bitcoin_policy"]
    descriptor = checkpoint_descriptor(policy)
    return dict(chain_id=chain_id, zk_state=state, bitcoin_network=policy["network"],
                bitcoin_policy_sha256=_digest(canonical(policy).encode()), checkpoint_descriptor=descriptor.hex(),
                program_image_id=policy["zk_spv"]["program_image_id"],
                confederation_id=policy["zk_spv"]["confederation_id"], verifier_version=VERIFIER_VERSION)


def _read_regular(path, limit, error):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                raise ValueError(error)
            result = stream.read(limit + 1)
            if len(result) != info.st_size:
                raise ValueError(error)
            return result
    except OSError:
        raise ValueError(error) from None


def _pack(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in (MANIFEST, JOURNAL, SEAL):
            if name in files:
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(entry, files[name])
    return output.getvalue()


def _unpack(data):
    error = "ZK_PEER_BUNDLE_ARCHIVE_INVALID"
    sizes = {MANIFEST: (1, MANIFEST_LIMIT), JOURNAL: (JOURNAL_SIZE, JOURNAL_SIZE), SEAL: (SEAL_SIZE, SEAL_SIZE)}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if (len(entries) != len(names) or names not in ({MANIFEST, JOURNAL}, {MANIFEST, JOURNAL, SEAL})):
                raise ValueError(error)
            files = {}
            for entry in entries:
                low, high = sizes[entry.filename]
                if (entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits != 0
                        or not low <= entry.file_size <= high or entry.compress_size != entry.file_size):
                    raise ValueError(error)
                files[entry.filename] = archive.read(entry)
    except (OSError, zipfile.BadZipFile, UnicodeError, NotImplementedError):
        raise ValueError(error) from None
    # A single canonical encoding also rejects hidden bytes in local headers,
    # prefixed or trailing data, symlinks, extras and ZIP comments.
    if _pack(files) != data:
        raise ValueError(error)
    return files


def _manifest(scope, attempt, files):
    return dict(schema=SCHEMA, scope=scope, attempt_id=attempt,
                kind="groth16" if SEAL in files else "preparation",
                files={name: _digest(content) for name, content in files.items() if name != MANIFEST})


def _validate_journal(config, journal, seal):
    validate_transition_journal(config["chain_id"], config["zk_state"], config["bitcoin_policy"], journal)
    if seal is not None and (len(seal) != SEAL_SIZE or seal[:4] != VERIFIER_SELECTOR):
        raise ValueError("ZK_VERIFIER_3_1_REQUIRED")


def check_export_destination(destination, *, config=None):
    path = Path(destination)
    if not path.name or path.name in (".", ".."):
        raise ValueError("ZK_PEER_BUNDLE_DESTINATION")
    try:
        parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("ZK_PEER_BUNDLE_DESTINATION") from None
    path = parent / path.name
    if not parent.is_dir():
        raise ValueError("ZK_PEER_BUNDLE_DESTINATION")
    if os.path.lexists(path):
        raise ValueError("ZK_PEER_BUNDLE_DESTINATION_EXISTS")
    if config is not None:
        private_dirs = [config["directory"]]
        if config.get("archive"):
            private_dirs.append(config["archive"]["directory"])
        if any(path.is_relative_to(Path(directory).resolve()) for directory in private_dirs):
            raise ValueError("ZK_PEER_BUNDLE_DESTINATION")
    return path


def _atomic_write(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".zk-peer-", delete=False) as output:
            temporary = output.name
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path, follow_symlinks=False)
    except FileExistsError:
        raise ValueError("ZK_PEER_BUNDLE_DESTINATION_EXISTS") from None
    except OSError:
        raise ValueError("ZK_PEER_BUNDLE_DESTINATION") from None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def _local_attempt(worker, attempt):
    active = worker.inbox.get("attempt")
    if attempt is None:
        if not active:
            raise ValueError("ZK_PEER_BUNDLE_ATTEMPT_REQUIRED")
        attempt = active["id"]
    attempt = _attempt_id(attempt)
    if not active or active["id"] != attempt:
        local = worker.inbox.get("local-proof:" + attempt)
        jobs = worker.inbox.get("job_attempts") or {}
        if not (local and local.get("proving_attempt_id") == attempt) and not any(
                attempt in attempts for attempts in jobs.values()):
            raise ValueError("ZK_PEER_BUNDLE_ATTEMPT_REQUIRED")
    attempts = worker.inbox.path / "attempts"
    base = attempts / attempt
    if attempts.is_symlink() or base.is_symlink() or not base.is_dir():
        raise ValueError("ZK_PEER_BUNDLE_ATTEMPT_REQUIRED")
    return attempt, base


def export_attempt(worker, destination, *, include_proof=True, attempt=None):
    """Export the exact local preparation and, if selected, its locally verified seal."""
    path = check_export_destination(destination, config=worker.c)
    scope = _scope(worker.c)
    with worker.exclusive():
        attempt, base = _local_attempt(worker, attempt)
        prover = LocalProver(worker.c, worker.lock_fd)
        preparation = prover.checked_preparation(base)
        journal = _read_regular(base / JOURNAL, JOURNAL_SIZE, "ZK_PEER_BUNDLE_JOURNAL_INVALID")
        if (preparation["journal_sha256"] != _digest(journal)
                or preparation["program_image_id"] != scope["program_image_id"]):
            raise ValueError("ZK_PEER_BUNDLE_PREPARATION_MISMATCH")
        _validate_journal(worker.c, journal, None)
        files = {JOURNAL: journal}
        if include_proof:
            private_key = worker.inbox.path / ".local-proof-key"
            local_result = base / "local-result"
            if (not private_key.is_file() or private_key.is_symlink() or local_result.is_symlink()
                    or not (local_result / "receipt.bin").is_file() or (local_result / "receipt.bin").is_symlink()):
                raise ValueError("ZK_LOCAL_PROOF_ORIGIN_REQUIRED")
            with tempfile.TemporaryDirectory(dir=base, prefix="bundle-verify-") as output:
                seal = prover.verify_own(base, Path(output))
            if _read_regular(base / JOURNAL, JOURNAL_SIZE, "ZK_PEER_BUNDLE_JOURNAL_INVALID") != journal:
                raise ValueError("ZK_PEER_BUNDLE_PREPARATION_MISMATCH")
            _validate_journal(worker.c, journal, seal)
            files[SEAL] = seal
        manifest = _manifest(scope, attempt, files)
        files[MANIFEST] = canonical(manifest).encode()
        data = _pack(files)
        _atomic_write(path, data)
    return dict(status="ZK_BUNDLE_EXPORTED", proving_attempt_id=attempt, bundle_file=str(path),
                bundle_sha256=_digest(data), kind=manifest["kind"], local_proof_verified=include_proof, published=False)


def _review_files(files, config, scope, require_proof):
    try:
        manifest = json.loads(files[MANIFEST])
        if not isinstance(manifest, dict) or canonical(manifest).encode() != files[MANIFEST]:
            raise ValueError()
    except (ValueError, RecursionError):
        raise ValueError("ZK_PEER_BUNDLE_MANIFEST_INVALID") from None
    if manifest.get("scope") != scope:
        raise ValueError("ZK_PEER_BUNDLE_SCOPE")
    attempt = _attempt_id(manifest.get("attempt_id"))
    if manifest != _manifest(scope, attempt, files):
        raise ValueError("ZK_PEER_BUNDLE_MANIFEST_INVALID")
    journal, seal = files[JOURNAL], files.get(SEAL)
    if require_proof and seal is None:
        raise ValueError("ZK_PEER_BUNDLE_PROOF_REQUIRED")
    _validate_journal(config, journal, seal)
    return attempt, journal, seal


def _review_context(current, journal, has_proof):
    commitment = bytes(current[7])
    before, after = journal[224:256], journal[256:288]
    if commitment == after:
        return "ALREADY_ACCEPTED", "RETAIN_REVIEW_EVIDENCE"
    if current[8]:
        return "DIFFERENT_STATE", "REVIEW_PERMANENT_HALT"
    if commitment != before:
        return "DIFFERENT_STATE", "PREPARE_FROM_CONFIRMED_STATE"
    return "MATCHES_PREDECESSOR", "REVIEW_WITH_FROST" if has_proof else "COMPUTE_LOCAL_PROOF"


def verify_bundle(worker, source, *, require_proof=False):
    """Review evidence via confirmed EVM reads without loading an operational worker."""
    config, w3 = worker.c, worker.w3
    scope = _scope(config)
    data = _read_regular(source, BUNDLE_LIMIT, "ZK_PEER_BUNDLE_ARCHIVE_INVALID")
    attempt, journal, seal = _review_files(_unpack(data), config, scope, require_proof)
    if w3.eth.chain_id != int(config["chain_id"]):
        raise ValueError("CHAIN_MISMATCH")
    block = read_block(w3, int(config["evm_confirmations"]))
    state, _, records, _ = inspect_binding(w3, config["zk_state"], config["bitcoin_policy"], block.number)
    if seal is not None:
        verifier = w3.eth.contract(address=records["zk_verifier"]["address"],
                                   abi=artifact("RiscZeroGroth16Verifier")["abi"])
        image = bytes.fromhex(scope["program_image_id"])
        journal_digest = hashlib.sha256(journal).digest()
        try:
            verifier.functions.verify(seal, image, journal_digest).call(block_identifier=block.number)
        except ContractLogicError:
            raise ValueError("ZK_PEER_BUNDLE_PROOF_INVALID") from None
    current = state.functions.getState().call(block_identifier=block.number)
    fresh = state.functions.isFresh().call(block_identifier=block.number)
    verify_anchor(w3, block.number, block.hash)
    relation, next_action = _review_context(current, journal, seal is not None)
    return dict(status="ZK_BUNDLE_PROOF_VERIFIED" if seal is not None else "ZK_BUNDLE_PREPARATION_REVIEWED",
                proving_attempt_id=attempt, bundle_sha256=_digest(data), journal_sha256=_digest(journal),
                scope=scope, proof_verified=seal is not None, publication_authorized=False, published=False,
                onchain_state_relation=relation, onchain_fresh=fresh, onchain_halted=current[8], next_action=next_action,
                evm_block_number=block.number, evm_block_hash="0x" + bytes(block.hash).hex())

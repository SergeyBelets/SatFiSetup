"""Portable evidence is public, strictly scoped and reviewed without a worker."""

from contextlib import nullcontext
import io
import json
from types import SimpleNamespace
import zipfile

from eth_account import Account
import pytest
from web3.exceptions import ContractLogicError

from signer_agent import zk_peer_bundle as bundle
from signer_agent.zk_local_proofs import LocalProofs
from signer_agent.zk_profiles import journal_network
from signer_agent.zk_spv import VERIFIER_SELECTOR, checkpoint_descriptor
from signer_agent.zk_worker_prover import LocalProver
from test_zk_bootstrap import zk_invite
from test_zk_testnet4 import zk_policy


ATTEMPT = "3e" * 32
BEFORE = b"\x01" * 32
AFTER = b"\x02" * 32
VERIFIER = "0x" + "22" * 20
BLOCK_HASH = b"\x42" * 32
SEAL = VERIFIER_SELECTOR + bytes(256)


@pytest.fixture(params=["mainnet", "testnet4"])
def scope(request):
    network = request.param
    policy = zk_invite()["bitcoin_policy"] if network == "mainnet" else zk_policy()
    config = dict(chain_id="1" if network == "mainnet" else "11155111",
                  evm_confirmations="12" if network == "mainnet" else "4",
                  zk_state="0x" + "11" * 20, bitcoin_policy=policy)
    words = [bytes(32) for _ in range(38)]
    words[:5] = (b"SATOSHIFI_TRANSITION_V2".ljust(32, b"\0"), journal_network(policy),
                 int(config["chain_id"]).to_bytes(32, "big"), bytes(12) + bytes.fromhex(config["zk_state"][2:]),
                 checkpoint_descriptor(policy))
    words[7], words[8] = BEFORE, AFTER
    words[20] = (1).to_bytes(32, "big")
    return config, b"".join(words)


@pytest.fixture
def local(scope, tmp_path):
    config, journal = scope
    directory = tmp_path / "worker"
    directory.mkdir(mode=0o700)
    config = dict(config, directory=str(directory), identity_file=str(directory / "identity.json"))
    identity = directory / "identity.json"
    identity.write_text(json.dumps(dict(private_key=Account.create().key.hex())))
    identity.chmod(0o600)
    base = directory / "attempts" / ATTEMPT
    base.mkdir(parents=True)
    (base / "state.input").write_bytes(b"private local input marker")
    (base / bundle.JOURNAL).write_bytes(journal)
    (base / "preparation.json").write_text(json.dumps(dict(source_sha256={},
        input_sha256=bundle._digest((base / "state.input").read_bytes()), journal_sha256=bundle._digest(journal),
        program_image_id=config["bitcoin_policy"]["zk_spv"]["program_image_id"])))
    records = {"attempt": {"id": ATTEMPT}}
    peer = SimpleNamespace(c=config, inbox=SimpleNamespace(path=directory, get=records.get),
                           exclusive=nullcontext, lock_fd=None)
    return peer, base, records


def add_local_proof(local):
    peer, base, _ = local
    output = base / "local-result"
    output.mkdir()
    receipt = output / "receipt.bin"
    receipt.write_bytes(b"private local receipt marker")
    authenticator = LocalProofs(peer.c)
    (output / "origin.json").write_text(json.dumps(authenticator.sign(authenticator.payload(base, receipt))))


def write_bundle(path, scope, seal=SEAL, *, rehash=True):
    config, journal = scope
    files = {bundle.JOURNAL: journal}
    if seal is not None:
        files[bundle.SEAL] = seal
    manifest = bundle._manifest(bundle._scope(config), ATTEMPT, files)
    if not rehash:
        manifest["files"][bundle.JOURNAL] = "00" * 32
    files[bundle.MANIFEST] = bundle.canonical(manifest).encode()
    path.write_bytes(bundle._pack(files))
    return files


def reviewer(monkeypatch, config, *, commitment=BEFORE, fresh=True, halted=False, fail_proof=False, fork=False):
    calls = []
    block = SimpleNamespace(number=100, hash=BLOCK_HASH)
    current = [None] * 12
    current[7], current[8] = commitment, halted

    def read(value):
        def call(*, block_identifier):
            assert block_identifier == block.number
            return value
        return SimpleNamespace(call=call)

    def verify(seal, image, digest):
        calls.append((seal, image, digest))
        if fail_proof:
            raise ContractLogicError("invalid proof")
        return read(None)

    def verifier(*, address, abi):
        assert address == VERIFIER
        assert any(item.get("name") == "verify" and item.get("stateMutability") == "view" for item in abi)
        return SimpleNamespace(functions=SimpleNamespace(verify=verify))

    def get_block(number):
        assert number == block.number
        return SimpleNamespace(number=number, hash=bytes(32) if fork else BLOCK_HASH)

    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=int(config["chain_id"]), contract=verifier, get_block=get_block))

    def binding(w3_, address, policy, height):
        assert w3_ is w3 and address == config["zk_state"] and policy == config["bitcoin_policy"]
        assert height == block.number
        state = SimpleNamespace(functions=SimpleNamespace(getState=lambda: read(current), isFresh=lambda: read(fresh)))
        return state, None, {"zk_verifier": {"address": VERIFIER}}, None

    monkeypatch.setattr(bundle, "read_block", lambda w3_, confirmations: block)
    monkeypatch.setattr(bundle, "inspect_binding", binding)
    return SimpleNamespace(c=config, w3=w3), calls


def test_preparation_export_and_verification_do_not_use_identity(local, scope, tmp_path, monkeypatch):
    writer, _, _ = local
    config, _ = scope
    path = tmp_path / "prepared.zip"
    exported = bundle.export_attempt(writer, path, include_proof=False)
    assert exported["status"] == "ZK_BUNDLE_EXPORTED" and not exported["local_proof_verified"]
    assert not (writer.inbox.path / ".local-proof-key").exists()
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == [bundle.MANIFEST, bundle.JOURNAL]
    peer, calls = reviewer(monkeypatch, config)
    result = bundle.verify_bundle(peer, path)
    assert result["status"] == "ZK_BUNDLE_PREPARATION_REVIEWED"
    assert result["onchain_state_relation"] == "MATCHES_PREDECESSOR"
    assert not result["proof_verified"] and not result["publication_authorized"] and not calls
    with pytest.raises(ValueError, match="ZK_PEER_BUNDLE_PROOF_REQUIRED"):
        bundle.verify_bundle(peer, path, require_proof=True)


@pytest.mark.parametrize("scenario", ["consistent", "state", "proof", "anchor"])
def test_witness_pins_same_confirmed_block_and_repeats_proof_verification(scope, tmp_path, monkeypatch, scenario):
    config, _ = scope
    config = dict(config, evm_rpc="https://first.example.test/secret",
                  evm_witness_rpc="https://second.example.test/secret")
    path = tmp_path / "proved.zip"
    write_bundle(path, scope)
    primary, first_calls = reviewer(monkeypatch, config)
    primary_binding = bundle.inspect_binding
    witness, second_calls = reviewer(monkeypatch, config, commitment=AFTER if scenario == "state" else BEFORE,
                                     fail_proof=scenario == "proof")
    witness_binding = bundle.inspect_binding

    def get_block(tip, fork=False):
        def block(number):
            if number == "latest":
                number = tip
            if number == "finalized":
                number = 100
            return SimpleNamespace(number=number, hash=bytes(32) if fork else BLOCK_HASH)
        return block

    primary.w3.eth.get_block = get_block(120)
    witness.w3.eth.get_block = get_block(100 + int(config["evm_confirmations"]) - 1, scenario == "anchor")
    monkeypatch.setattr(bundle, "witness_web3", lambda c, *, timeout: witness.w3)
    monkeypatch.setattr(bundle, "inspect_binding", lambda w3, *a:
                        (primary_binding if w3 is primary.w3 else witness_binding)(w3, *a))
    if scenario != "consistent":
        reason = "ZK_PEER_BUNDLE_PROOF_INVALID" if scenario == "proof" else "EVM_RPC_WITNESS_MISMATCH"
        with pytest.raises(ValueError, match=f"^{reason}$"):
            bundle.verify_bundle(primary, path, require_proof=True)
        assert bool(first_calls) == bool(second_calls) == (scenario != "anchor")
    else:
        review = bundle.verify_bundle(primary, path, require_proof=True)
        assert review["proof_verified"] and review["independent_rpc_confirmed"]
        assert review["evm_block_number"] == 100
        assert review["onchain_state_relation"] == "MATCHES_PREDECESSOR"
        assert first_calls == second_calls and len(first_calls) == 1
        assert "secret" not in json.dumps(review)


def test_exact_local_proof_can_be_exported_after_attempt_is_cleared(local, scope, tmp_path, monkeypatch):
    writer, base, records = local
    config, journal = scope
    add_local_proof(local)
    records["attempt"] = None
    records["local-proof:" + ATTEMPT] = {"proving_attempt_id": ATTEMPT}

    def verify(self, actual_base, receipt, output):
        assert actual_base == base and receipt == base / "local-result/receipt.bin"
        assert output.parent == base
        return SEAL

    monkeypatch.setattr(LocalProver, "verify", verify)
    path = tmp_path / "proved.zip"
    exported = bundle.export_attempt(writer, path, attempt=ATTEMPT)
    assert exported["local_proof_verified"] and not exported["published"]
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == [bundle.MANIFEST, bundle.JOURNAL, bundle.SEAL]
        assert archive.read(bundle.JOURNAL) == journal and archive.read(bundle.SEAL) == SEAL
    assert not list(base.glob("bundle-verify-*"))
    peer, calls = reviewer(monkeypatch, config, commitment=AFTER)
    result = bundle.verify_bundle(peer, path, require_proof=True)
    assert result["status"] == "ZK_BUNDLE_PROOF_VERIFIED" and result["proof_verified"]
    assert result["onchain_state_relation"] == "ALREADY_ACCEPTED" and not result["publication_authorized"]
    assert result["bundle_sha256"] == exported["bundle_sha256"]
    assert calls == [(SEAL, bytes.fromhex(config["bitcoin_policy"]["zk_spv"]["program_image_id"]),
                      bytes.fromhex(bundle._digest(journal)))]


def test_export_requires_authentic_local_origin(local, tmp_path, monkeypatch):
    writer, base, _ = local
    path = tmp_path / "proved.zip"
    with pytest.raises(ValueError, match="ZK_LOCAL_PROOF_ORIGIN_REQUIRED"):
        bundle.export_attempt(writer, path)
    assert not (writer.inbox.path / ".local-proof-key").exists()
    add_local_proof(local)
    (base / "local-result/receipt.bin").write_bytes(b"a different receipt")
    monkeypatch.setattr(LocalProver, "verify", lambda *args: pytest.fail("rejected receipt reached the verifier"))
    with pytest.raises(ValueError, match="ZK_LOCAL_PROOF_ORIGIN_MISMATCH"):
        bundle.export_attempt(writer, path)
    assert not path.exists()


def test_review_handles_advanced_and_halted_state(scope, tmp_path, monkeypatch):
    path = tmp_path / "proved.zip"
    write_bundle(path, scope)
    config, _ = scope
    for halted, action in [(False, "PREPARE_FROM_CONFIRMED_STATE"), (True, "REVIEW_PERMANENT_HALT")]:
        peer, calls = reviewer(monkeypatch, config, commitment=bytes(32), fresh=False, halted=halted)
        result = bundle.verify_bundle(peer, path)
        assert result["proof_verified"] and not result["onchain_fresh"] and not result["publication_authorized"]
        assert result["onchain_halted"] == halted and result["next_action"] == action
        assert result["onchain_state_relation"] == "DIFFERENT_STATE" and len(calls) == 1


def test_review_rejects_revert_and_fork(scope, tmp_path, monkeypatch):
    path = tmp_path / "proved.zip"
    write_bundle(path, scope)
    config, _ = scope
    for options, error in [(dict(fail_proof=True), "ZK_PEER_BUNDLE_PROOF_INVALID"),
                           (dict(fork=True), "EVM_REORG_RECOVERY_REQUIRED")]:
        peer, _ = reviewer(monkeypatch, config, **options)
        with pytest.raises(ValueError, match=error):
            bundle.verify_bundle(peer, path)


def test_review_rejects_wrong_scope_and_digest_without_rpc(scope, tmp_path):
    path = tmp_path / "proved.zip"
    write_bundle(path, scope)
    config, _ = scope
    wrong = dict(config, zk_state="0x" + "22" * 20)
    with pytest.raises(ValueError, match="ZK_PEER_BUNDLE_SCOPE"):
        bundle.verify_bundle(SimpleNamespace(c=wrong, w3=None), path)
    write_bundle(path, scope, rehash=False)
    with pytest.raises(ValueError, match="ZK_PEER_BUNDLE_MANIFEST_INVALID"):
        bundle.verify_bundle(SimpleNamespace(c=config, w3=None), path)


def test_review_rejects_journal_deployment_mismatch_even_if_rehashed(scope, tmp_path):
    path = tmp_path / "proved.zip"
    config, journal = scope
    other_contract = journal[:96] + bytes(12) + bytes.fromhex("33" * 20) + journal[128:]
    write_bundle(path, (config, other_contract))
    with pytest.raises(ValueError, match="ZK_PROOF_DEPLOYMENT_MISMATCH"):
        bundle.verify_bundle(SimpleNamespace(c=config, w3=None), path)


def test_review_rejects_non_whitelisted_or_noncanonical_zip(scope, tmp_path):
    path = tmp_path / "proved.zip"
    files = write_bundle(path, scope)
    original = path.read_bytes()
    config, _ = scope
    invalid = [original + b"trailing metadata", b"prefix" + original, bytes(bundle.BUNDLE_LIMIT + 1)]
    for name, content in [("../state.journal", files[bundle.JOURNAL]), (".local-proof-key", b"unknown")]:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for filename, value in files.items():
                archive.writestr(filename, value)
            archive.writestr(name, content)
        invalid.append(output.getvalue())
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, value in files.items():
            archive.writestr(filename, value)
    invalid.append(output.getvalue())
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for filename, value in files.items():
            archive.writestr(filename, value)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr(bundle.JOURNAL, files[bundle.JOURNAL])
    invalid.append(output.getvalue())
    for data in invalid:
        path.write_bytes(data)
        with pytest.raises(ValueError, match="ZK_PEER_BUNDLE_ARCHIVE_INVALID"):
            bundle.verify_bundle(SimpleNamespace(c=config, w3=None), path)


def test_export_does_not_replace_destination_or_write_inside_worker(local, tmp_path):
    writer, _, _ = local
    for destination in (tmp_path / "present.zip", tmp_path / "link.zip"):
        if destination.name == "present.zip":
            destination.write_bytes(b"original")
        else:
            destination.symlink_to(tmp_path / "never-created")
        with pytest.raises(ValueError, match="ZK_PEER_BUNDLE_DESTINATION_EXISTS"):
            bundle.export_attempt(writer, destination, include_proof=False)
    assert (tmp_path / "present.zip").read_bytes() == b"original"
    assert (tmp_path / "link.zip").is_symlink() and not (tmp_path / "never-created").exists()
    with pytest.raises(ValueError, match="ZK_PEER_BUNDLE_DESTINATION"):
        bundle.export_attempt(writer, writer.inbox.path / "copy.zip", include_proof=False)

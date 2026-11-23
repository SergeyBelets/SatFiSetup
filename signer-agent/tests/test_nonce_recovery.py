import pytest
from signer_agent.secure_state import SecureState


def test_spent_nonce_stale_backup_is_blocked(tmp_path):
    path = tmp_path / "state.enc"
    s = SecureState(str(path), "test-only")
    original = dict(
        status="nonce-created", message_hash="11" * 32, hiding="01", binding="02"
    )
    s.put("s", original)
    backup = path.read_bytes()
    s.prepare_share("s", "22" * 32, 123)
    assert "hiding" not in s.get("s")
    path.write_bytes(backup)
    with pytest.raises(RuntimeError, match="NONCE_ALREADY_SPENT"):
        s.assert_unspent("s")


def test_concurrent_process_lock_rejects(tmp_path):
    s = SecureState(str(tmp_path / "s.enc"), "test-only")
    other = SecureState(str(tmp_path / "s.enc"), "test-only")
    with s.exclusive():
        with pytest.raises(BlockingIOError):
            with other.exclusive():
                pass


def test_retry_retains_exact_share_and_transcript(tmp_path):
    s = SecureState(str(tmp_path / "s.enc"), "test-only")
    s.put(
        "s",
        dict(status="nonce-created", message_hash="11" * 32, hiding="01", binding="02"),
    )
    s.prepare_share("s", "22" * 32, 123)
    s.mark_consumed("s", "tx")
    data = s.get("s")
    assert data["transcript"] == "22" * 32 and int(data["share"], 16) == 123
    assert "hiding" not in data and "binding" not in data


@pytest.mark.parametrize("guarded", [False, True])
def test_network_crash_occurs_only_after_durable_share_and_retry_does_not_sign_again(
    tmp_path, monkeypatch, guarded
):
    import test_signing_service as fixture
    from signer_agent.service import SigningService
    from signer_agent import crypto

    captured = {}
    approve = SigningService.approve
    send = fixture.FakeClient.submit_signature_share

    def capture(self, session):
        captured.update(service=self, session=session)
        return approve(self, session)

    def crash(client, session_id, share):
        service = captured["service"]
        key = service.state.session_key(client.chain_id, client.address, session_id)
        stored = service.state.get(key)
        assert (
            stored["status"] == "share-prepared" and int(stored["share"], 16) == share
        )
        assert "hiding" not in stored and "binding" not in stored
        raise RuntimeError("TEST_NETWORK_CRASH")

    monkeypatch.setattr(SigningService, "approve", capture)
    monkeypatch.setattr(fixture.FakeClient, "submit_signature_share", crash)
    with pytest.raises(RuntimeError, match="TEST_NETWORK_CRASH"):
        fixture.test_two_round_approval_can_resume_across_runs(tmp_path, False, guarded)
    monkeypatch.setattr(fixture.FakeClient, "submit_signature_share", send)

    def forbidden(*args, **kwargs):
        raise AssertionError("nonce signed twice")

    monkeypatch.setattr(crypto, "call", forbidden)
    assert captured["service"].approve(captured["session"]).status == "signed"


def test_missing_or_substituted_independent_journal_blocks_old_nonce(tmp_path):
    import shutil

    path = tmp_path / "state.enc"
    state = SecureState(str(path), "test-only")
    state.put(
        "nonce",
        dict(status="nonce-created", message_hash="11" * 32, hiding="01", binding="02"),
    )
    journal = tmp_path / "state.enc.spent.sqlite"
    journal.unlink()
    with pytest.raises(RuntimeError, match="JOURNAL_MISSING_RECOVERY_REQUIRED"):
        state.assert_unspent("nonce")
    other = SecureState(str(tmp_path / "other.enc"), "test-only")
    other.assert_unspent("unused")
    shutil.copyfile(tmp_path / "other.enc.spent.sqlite", journal)
    with pytest.raises(RuntimeError, match="JOURNAL_MISMATCH_RECOVERY_REQUIRED"):
        state.assert_unspent("nonce")


@pytest.fixture
def guarded_state(tmp_path):
    ledger_directory = tmp_path / "external"
    ledger_directory.mkdir(mode=0o700)
    ledger = ledger_directory / "spent.sqlite"
    state = SecureState(tmp_path / "local" / "state.enc", "test-only", nonce_ledger=ledger)
    state.initialize_nonce_ledger()
    state.initialize_nonce_ledger()
    return state, ledger


def test_spent_ledger_survives_local_snapshot_restore(guarded_state, tmp_path):
    state, ledger = guarded_state
    saved = dict(status="nonce-created", message_hash="11" * 32, hiding="01", binding="02")
    state.put("nonce", saved)
    stale = state.path.read_bytes()
    state.prepare_share("nonce", "22" * 32, 123)
    state.path.write_bytes(stale)
    backup = SecureState(state.path, "test-only")  # Missing config cannot disable the recorded guard.

    # An unrelated journal next to the restored state cannot substitute for the guard.
    local = SecureState(tmp_path / "legacy" / "state.enc", "test-only")
    local.assert_unspent("unused")
    from shutil import copyfile

    copyfile(str(local.path) + ".spent.sqlite", str(backup.path) + ".spent.sqlite")
    with pytest.raises(RuntimeError, match="NONCE_ALREADY_SPENT"):
        backup.assert_unspent("nonce")

    old_ledger = ledger.rename(ledger.with_name("original.sqlite"))
    with pytest.raises(RuntimeError, match="NONCE_LEDGER_MISSING"):
        backup.assert_unspent("nonce")
    with pytest.raises(RuntimeError, match="NONCE_LEDGER_MISSING"):
        state.initialize_nonce_ledger()  # The original binding cannot be provisioned again.
    assert not ledger.exists()
    copyfile(str(local.path) + ".spent.sqlite", ledger)
    with pytest.raises(RuntimeError, match="NONCE_LEDGER_MISMATCH"):
        backup.assert_unspent("nonce")
    old_ledger.replace(ledger)
    backup.assert_spent("nonce", "22" * 32)


def test_cloned_signers_share_atomic_spent_fence(guarded_state, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    state, _ = guarded_state
    state.put("nonce", dict(status="nonce-created", message_hash="11" * 32, hiding="01", binding="02"))
    copied = SecureState(tmp_path / "clone" / "state.enc", "test-only")
    copied.path.write_bytes(state.path.read_bytes())
    ready = Barrier(2)

    def prepare(item):
        system, value = item
        with system.exclusive():
            system.assert_unspent("nonce")
            ready.wait(timeout=5)
            try:
                system.prepare_share("nonce", f"{value:064x}", value)
            except RuntimeError as error:
                assert str(error) == "NONCE_ALREADY_SPENT"
                return None
            return value

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(prepare, [(state, 1), (copied, 2)]))
    assert sum(item is not None for item in result) == 1
    winner = next(item for item in result if item is not None)
    for value, clone in enumerate((state, copied), start=1):
        clone.assert_spent("nonce", f"{winner:064x}")
        assert clone.get("nonce")["status"] == ("share-prepared" if value == winner else "nonce-created")


def test_explicit_guard_refuses_to_initialize_against_legacy_state(guarded_state, tmp_path):
    state, ledger = guarded_state
    other = ledger.with_name("other.sqlite")
    with pytest.raises(RuntimeError, match="NONCE_LEDGER_MISMATCH"):
        SecureState(state.path, "test-only", nonce_ledger=other).assert_unspent("nonce")
    previous = tmp_path / "older" / "state.enc"
    SecureState(previous, "test-only").put("key", {"stage": "active"})
    legacy = SecureState(previous, "test-only", nonce_ledger=other)
    with pytest.raises(RuntimeError, match="NONCE_LEDGER_FRESH_STATE_REQUIRED"):
        legacy.initialize_nonce_ledger()
    with pytest.raises(RuntimeError, match="NONCE_LEDGER_NOT_BOUND"):
        legacy.assert_unspent("nonce")
    assert not other.exists()


def test_nonce_ledger_cli_requires_private_external_storage(tmp_path, capsys):
    from signer_agent.participant_cli import main

    password = tmp_path / "password"
    password.write_text("11" * 32)
    password.chmod(0o600)
    state = tmp_path / "state" / "signer.enc"
    directory = tmp_path / "ledger"
    directory.mkdir(mode=0o755)
    args = ["nonce-ledger-init", "--state", str(state), "--password-file", str(password), "--nonce-ledger"]
    assert main([*args, str(state.parent / "adjacent.sqlite")]) != 0
    assert "NONCE_LEDGER_PATH" in capsys.readouterr().out
    assert main([*args, str(directory / "guard.sqlite")]) != 0
    assert "NONCE_LEDGER_DIRECTORY_PERMISSIONS" in capsys.readouterr().out
    directory.chmod(0o700)
    assert main([*args, str(directory / "guard.sqlite")]) == 0
    assert "NONCE_LEDGER_READY" in capsys.readouterr().out


def test_evm_nonce_ledger_cli_binds_actual_signing_state(tmp_path, capsys):
    from signer_agent.participant_cli import main

    secret = "11" * 32
    password = tmp_path / "password"
    password.write_text(secret)
    password.chmod(0o600)
    path = tmp_path / "state" / "evm.enc"
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    ledger = external / "guard.sqlite"
    args = ["nonce-ledger-init", "--evm-auth", "--state", str(path), "--password-file",
            str(password), "--nonce-ledger", str(ledger)]
    assert main(args) == 0
    capsys.readouterr()
    role = SecureState(path, secret)
    signer = SecureState(str(path) + ".signing", secret)
    assert role.evm_nonce_ledger(signer) == str(ledger)
    signer.put("signed", dict(status="nonce-created", message_hash="11" * 32, hiding="01", binding="02"))
    old_state = signer.path.read_bytes()
    signer.record_spent("signed", "22" * 32)
    signer.path.write_bytes(old_state)
    assert main(args) == 0
    role.assert_spent("signed", "22" * 32)
    with pytest.raises(RuntimeError, match="NONCE_ALREADY_SPENT"):
        signer.assert_unspent("signed")
    assert not signer.path.with_suffix(signer.path.suffix + ".spent.sqlite").exists()


@pytest.mark.parametrize("action,purpose,code", [
    ("sign-evm", "EVM_AUTH", "NONCE_LEDGER_NOT_BOUND_RECOVERY_REQUIRED"),
    ("btc-commit", "BTC_SPEND", "NONCE_LEDGER_NOT_BOUND_RECOVERY_REQUIRED"),
    ("btc-sign", "BTC_SPEND", "NONCE_LEDGER_NOT_BOUND_RECOVERY_REQUIRED"),
    ("import", "BTC_SPEND", "MAINNET_KEY_IMPORT_REQUIRES_ORIGINAL_STATE"),
])
def test_mainnet_role_never_falls_back_to_adjacent_ledger(tmp_path, monkeypatch, capsys, action, purpose, code):
    import json
    from web3 import Web3
    from signer_agent import participant_cli
    from signer_agent.dkg_protocol import Manifest

    addresses = tuple(Web3.to_checksum_address(f"0x{i:040x}") for i in (1, 2, 3))
    manifest = Manifest(1, addresses[2], b"s" * 32, 1, purpose, addresses[:2], (b"a" * 32, b"b" * 32), 2)
    bundle = tmp_path / "manifest.json"
    bundle.write_text(json.dumps(manifest.to_dict()))
    password = tmp_path / "password"
    password.write_text("11" * 32)
    password.chmod(0o600)
    state = tmp_path / "old.enc"
    SecureState(state, "11" * 32).put("existing", dict(stage="active"))
    before = state.read_bytes()
    monkeypatch.setattr(participant_cli, "Registry",
                        lambda *_args: pytest.fail("unguarded mainnet command attempted to access RPC"))
    assert participant_cli.main([action, "--manifest", str(bundle), "--state", str(state),
                                 "--password-file", str(password)]) != 0
    assert code in capsys.readouterr().out
    assert state.read_bytes() == before
    assert not (tmp_path / "old.enc.spent.sqlite").exists()
    assert not (tmp_path / "old.enc.signing.spent.sqlite").exists()


def test_enrolled_role_cannot_use_the_local_legacy_payment_file_path(tmp_path, monkeypatch, capsys):
    import json
    from web3 import Web3
    from signer_agent import participant_cli
    from signer_agent.dkg_protocol import Manifest

    addresses = tuple(Web3.to_checksum_address(f"0x{i:040x}") for i in (1, 2, 3))
    manifest = Manifest(31337, addresses[2], b"s" * 32, 1, "EVM_AUTH",
                        addresses[:2], (b"a" * 32, b"b" * 32), 2)
    bundle = tmp_path / "manifest.json"
    bundle.write_text(json.dumps(manifest.to_dict()))
    password = tmp_path / "password"
    password.write_text("11" * 32)
    password.chmod(0o600)
    state = tmp_path / "role.enc"
    SecureState(state, "11" * 32).put("enrollment", dict(participant=addresses[0]))
    before = state.read_bytes()
    monkeypatch.setattr(participant_cli, "Registry",
                        lambda *_args: pytest.fail("enrolled role skipped invitation verification"))
    assert participant_cli.main(["sign-evm", "--manifest", str(bundle), "--state", str(state),
                                 "--password-file", str(password), "--package", str(tmp_path / "test.json")]) == 2
    assert "ENROLLED_MANIFEST_REQUIRED" in capsys.readouterr().out
    assert state.read_bytes() == before


@pytest.mark.parametrize("companion", ["missing", "legacy", "other_ledger"])
def test_sign_evm_cli_rejects_unverified_companion_before_rpc(tmp_path, monkeypatch, capsys, companion):
    from signer_agent import participant_cli

    secret = "11" * 32
    password = tmp_path / "password"
    password.write_text(secret)
    password.chmod(0o600)
    path = tmp_path / "state" / "evm.enc"
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    ledger = external / "role.sqlite"
    role = SecureState(path, secret, nonce_ledger=ledger)
    role.initialize_nonce_ledger()
    signer_path = str(path) + ".signing"
    if companion == "legacy":
        SecureState(signer_path, secret).assert_unspent("untouched")
    if companion == "other_ledger":
        SecureState(signer_path, secret, nonce_ledger=external / "other.sqlite").initialize_nonce_ledger()

    def no_network(*_args, **_kwargs):
        pytest.fail("sign-evm reached RPC with an unverified ledger binding")

    monkeypatch.setattr("signer_agent.evm_rpc.evm_web3", no_network)
    arguments = ["sign-evm", "--state", str(path), "--password-file", str(password),
                 "--board", "unused", "--rpc", "http://localhost:8545"]
    expected = ("NONCE_LEDGER_MISMATCH_RECOVERY_REQUIRED" if companion == "other_ledger"
                else "EVM_NONCE_LEDGER_NOT_BOUND_RECOVERY_REQUIRED")
    for flags in ([], ["--nonce-ledger", str(ledger)]):
        assert participant_cli.main([*arguments, *flags]) == 2
        assert expected in capsys.readouterr().out
    with pytest.raises(RuntimeError, match=expected):
        role.initialize_nonce_ledger(evm_signing_state=SecureState(signer_path, secret))
    if companion == "missing":
        assert not SecureState(signer_path, secret).path.exists()

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


def test_network_crash_occurs_only_after_durable_share_and_retry_does_not_sign_again(
    tmp_path, monkeypatch
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
        fixture.test_two_round_approval_can_resume_across_runs(tmp_path, False)
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

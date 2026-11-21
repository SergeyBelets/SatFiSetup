import os

import pytest

from signer_agent.secure_state import SecureState


def test_state_is_encrypted_and_nonce_is_removed_after_consumption(tmp_path):
    path = tmp_path / "state.enc"
    state = SecureState(str(path), "test-secret-with-enough-entropy")
    key = state.session_key(1, "0xabc", 7)
    state.put(
        key,
        {
            "status": "nonce-created",
            "message_hash": "11" * 32,
            "hiding": "0x1234",
            "binding": "0x5678",
        },
    )

    ciphertext = path.read_bytes()
    assert b"1234" not in ciphertext
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert state.get(key)["hiding"] == "0x1234"

    state.mark_consumed(key, "0xtx")
    consumed = state.get(key)
    assert consumed["status"] == "consumed"
    assert "hiding" not in consumed
    assert "binding" not in consumed


def test_wrong_state_key_is_rejected(tmp_path):
    path = tmp_path / "state.enc"
    SecureState(str(path), "first-key").put("key", {"status": "nonce-created"})
    with pytest.raises(RuntimeError, match="cannot decrypt signer state"):
        SecureState(str(path), "second-key").get("key")

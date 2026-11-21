from copy import deepcopy
import pytest
from signer_agent.mint_attempts import select_attempt, MAX_ATTEMPTS
from signer_agent.secure_state import SecureState


def intent(digest="11" * 32):
    return dict(chain_id="1", coordinator="coordinator", role="EVM_AUTH", target="pool",
                artifact="MiningPoolV1", hash_method="getMintOperationHash",
                hash_args=["txid", "0", ["recipient"], ["10000000000"]], operation_hash=digest)


def test_nonce_rebase_retains_original_intent_and_durable_spent_journal(tmp_path):
    state = SecureState(str(tmp_path / "state.enc"), "test-only")
    expected = intent()
    with state.exclusive():
        first_key, cached = select_attempt(state, "deposit", expected, 4, 1000)
        assert cached is None
        first = dict(session="7", intent=dict(expected, session="7"), anchor=dict(number="100", hash="abc"))
        state.put(first_key, first)
        state.put("signing:7", dict(status="nonce-created", message_hash="11" * 32))
        state.prepare_share("signing:7", "22" * 32, 3)
        journal = state.get("signing:7")
        second_key, cached = select_attempt(state, "deposit", intent("22" * 32), 5, 1000)
        assert cached is None and second_key != first_key
        assert state.get(first_key) == first
        assert state.get("signing:7") == journal
        assert state.get("deposit:attempts-v1")["attempts"][0]["status"] == "STALE_CONTRACT_NONCE"
        assert select_attempt(state, "deposit", intent("22" * 32), 5, 1000) == (second_key, None)
    restored = SecureState(str(tmp_path / "state.enc"), "test-only")
    assert restored.get(first_key) == first
    assert restored.get("signing:7") == journal


def test_legacy_cache_migrates_without_deleting_signed_record(tmp_path):
    state = SecureState(str(tmp_path / "state.enc"), "test-only")
    old = dict(session="7", intent=dict(intent(), session="7"), anchor=dict(number="100", hash="abc"))
    state.put("deposit", old)
    with pytest.raises(ValueError, match="LEGACY_NONCE_OBSERVATION_REQUIRED"):
        select_attempt(state, "deposit", intent("22" * 32), 5, 1000)
    key, _ = select_attempt(state, "deposit", intent("22" * 32), 5, 1000, legacy_nonce=4, legacy_deadline=1000)
    assert state.get("deposit") == old
    rows = state.get("deposit:attempts-v1")["attempts"]
    assert len(rows) == 2 and rows[1]["cache_key"] == key
    assert state.get(rows[0]["cache_key"]) == old


def test_rebase_does_not_permit_changed_money_deadline_or_unknown_generation(tmp_path):
    state = SecureState(str(tmp_path / "state.enc"), "test-only")
    select_attempt(state, "deposit", intent(), 4, 1000)
    changed = deepcopy(intent()); changed["hash_args"][2] = ["attacker"]
    with pytest.raises(ValueError, match="ECONOMIC_INTENT_CHANGED"):
        select_attempt(state, "deposit", changed, 5, 1000)
    with pytest.raises(ValueError, match="ORIGINAL_DEADLINE_REQUIRED"):
        select_attempt(state, "deposit", intent(), 4, 1001)
    with pytest.raises(ValueError, match="CANONICAL_NONCE_REGRESSION"):
        select_attempt(state, "deposit", intent(), 3, 1000)
    with pytest.raises(ValueError, match="AUTHORIZATION_CONTEXT_CHANGED"):
        select_attempt(state, "deposit", intent("22" * 32), 4, 1000)


def test_paid_attempt_count_is_bounded(tmp_path):
    state = SecureState(str(tmp_path / "state.enc"), "test-only")
    for nonce in range(MAX_ATTEMPTS):
        select_attempt(state, "deposit", intent(f"{nonce:064x}"), nonce, 1000)
    with pytest.raises(ValueError, match="ATTEMPT_BUDGET_EXHAUSTED"):
        select_attempt(state, "deposit", intent("ff" * 32), MAX_ATTEMPTS, 1000)
    assert len(state.get("deposit:attempts-v1")["attempts"]) == MAX_ATTEMPTS

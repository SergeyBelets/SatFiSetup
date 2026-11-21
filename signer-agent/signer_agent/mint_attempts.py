"""Durable authorization attempts; never rewrite a signed intent or nonce journal.

This resolves local recovery only. It is not cross-host canonical admission for
the pool nonce; the on-chain monetary nonce remains the execution boundary.
"""
from copy import deepcopy
from eth_utils import keccak
from .operations import canonical

MAX_ATTEMPTS = 8


def select_attempt(state, key, expected, operation_nonce, deadline, *, legacy_nonce=None,
                   legacy_deadline=None, legacy_verified=False):
    """Call under SecureState.exclusive after exact-result/canonical reads.

    The first deadline is immutable. Exhaustion requires explicit investigation,
    never automatic deletion of state or unlimited paid FROST retries.
    """
    economic = {name: value for name, value in expected.items() if name != "operation_hash"}
    index_key = key + ":attempts-v1"
    index = state.get(index_key)
    if index is None:
        index = dict(version="1", economic=economic, deadline=str(deadline), attempts=[])
        legacy = state.get(key)
        if legacy is not None:
            old = legacy["intent"]
            if {name: value for name, value in old.items() if name not in ("operation_hash", "session")} != economic:
                raise ValueError("BTC_MINT_ECONOMIC_INTENT_CHANGED")
            if (legacy_nonce is None and not legacy_verified) or legacy_deadline is None:
                raise ValueError("BTC_MINT_LEGACY_NONCE_OBSERVATION_REQUIRED")
            if int(deadline) != int(legacy_deadline):
                raise ValueError("BTC_MINT_ORIGINAL_DEADLINE_REQUIRED")
            old_key = key + ":attempt:" + old["operation_hash"]
            state.put(old_key, deepcopy(legacy))
            index["attempts"].append(dict(id=old["operation_hash"], cache_key=old_key,
                operation_hash=old["operation_hash"],
                operation_nonce=None if legacy_nonce is None else str(legacy_nonce),
                status="LEGACY_NONCE_UNKNOWN" if legacy_nonce is None else "ACTIVE"))
    if index["economic"] != economic:
        raise ValueError("BTC_MINT_ECONOMIC_INTENT_CHANGED")
    if int(index["deadline"]) != int(deadline):
        raise ValueError("BTC_MINT_ORIGINAL_DEADLINE_REQUIRED")
    attempts = index["attempts"]
    if attempts:
        old = attempts[-1]
        old_nonce = None if old["operation_nonce"] is None else int(old["operation_nonce"])
        if old_nonce is not None and int(operation_nonce) < old_nonce:
            raise ValueError("BTC_MINT_CANONICAL_NONCE_REGRESSION")
        if old["operation_hash"] == expected["operation_hash"]:
            if int(operation_nonce) != old_nonce:
                raise ValueError("BTC_MINT_NONCE_HASH_MISMATCH")
            state.put(index_key, index)
            return old["cache_key"], state.get(old["cache_key"])
        if old_nonce is not None and int(operation_nonce) == old_nonce:
            raise ValueError("BTC_MINT_AUTHORIZATION_CONTEXT_CHANGED")
        if old_nonce is not None:
            old["status"] = "STALE_CONTRACT_NONCE"
    if len(attempts) >= MAX_ATTEMPTS:
        state.put(index_key, index)
        raise ValueError("BTC_MINT_ATTEMPT_BUDGET_EXHAUSTED")
    attempt_id = keccak(canonical(dict(economic=economic, operation_hash=expected["operation_hash"],
        operation_nonce=str(operation_nonce), deadline=str(deadline))).encode()).hex()
    cache_key = key + ":attempt:" + attempt_id
    attempts.append(dict(id=attempt_id, operation_hash=expected["operation_hash"],
        operation_nonce=str(operation_nonce), cache_key=cache_key, status="ACTIVE"))
    state.put(index_key, index)
    return cache_key, state.get(cache_key)

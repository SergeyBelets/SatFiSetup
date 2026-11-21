from types import SimpleNamespace

import pytest
from hexbytes import HexBytes

from signer_agent.gas_caller import eligible
from signer_agent.gas_caller_budget import CallerBudget, validate_budget


POLICY = dict(period_seconds="3600", max_period_cost_wei="1000000",
    max_transactions_per_period="4", max_attempts_per_payee_epoch="3",
    retry_backoff_seconds="60", gas_limit="100000", max_fee_per_gas_wei="2",
    max_priority_fee_per_gas_wei="1", min_caller_balance_wei="100",
    min_claim_to_fee_ratio="2")


def budget(tmp_path, **policy):
    return CallerBudget(tmp_path / "budget.sqlite", dict(POLICY, **policy), "31337", "caller", "1")


@pytest.mark.parametrize("field,value", [
    ("gas_limit", "16777217"), ("gas_limit", "20999"), ("gas_limit", 100000),
    ("period_seconds", "59"), ("max_fee_per_gas_wei", "0"),
    ("max_period_cost_wei", "199999"), ("max_priority_fee_per_gas_wei", "3"),
    ("max_attempts_per_payee_epoch", "0"), ("retry_backoff_seconds", "0"),
    ("max_transactions_per_period", "1025"), ("min_claim_to_fee_ratio", "0"),
    ("min_caller_balance_wei", "-1"), ("gas_limit", "0100000"),
    ("max_period_cost_wei", str(2**256)),
])
def test_explicit_policy_bounds(field, value):
    with pytest.raises(ValueError, match="GAS_CALLER_POLICY"):
        validate_budget(dict(POLICY, **{field: value}))


def test_no_implicit_or_unrecognized_caps():
    for value in ({}, dict(POLICY, target="arbitrary")):
        with pytest.raises(ValueError, match="GAS_CALLER_POLICY"):
            validate_budget(value)


@pytest.mark.parametrize("balance,claim,expected", [
    (1000000, 0, "FUNDED"), (0, 399999, "CLAIM_TOO_SMALL"),
    (0, 900000, "INSUFFICIENT_ENTITLEMENT"), (0, 1000000, "READY"),
    (600000, 400000, "READY"), (900000, 100000, "CLAIM_TOO_SMALL"),
])
def test_actual_funding_and_economic_thresholds(balance, claim, expected):
    payee = dict(balance_floor_wei="1000000", min_claim_wei="100")
    assert eligible(balance, claim, payee, validate_budget(POLICY), 200000) == expected


def test_policy_cannot_reset_budget(tmp_path):
    budget(tmp_path)
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        budget(tmp_path, max_period_cost_wei="2000000")


def test_global_budget_survives_rotation_and_restart(tmp_path):
    item = budget(tmp_path, max_period_cost_wei="300000")
    first = item.reserve("scope:1:alice", 100, {})
    item.finish(first, dict(cost_wei="150000", cost_period="0"))
    restarted = budget(tmp_path, max_period_cost_wei="300000")
    assert restarted.limits("scope:2:bob", 101) == "PERIOD_BUDGET"
    assert restarted.limits("scope:2:bob", 3601) is None


def test_pending_reservation_is_carried_into_new_period(tmp_path):
    item = budget(tmp_path, max_period_cost_wei="300000")
    pending = item.reserve("scope:1:alice", 3599, {})
    assert item.limits("scope:2:bob", 3601) == "PERIOD_BUDGET"
    item.journal.update(pending["id"], "SETTLING", dict(cost_wei="1", cost_period="1"))
    assert item.limits("scope:2:bob", 3601) == "PERIOD_BUDGET"
    item.finish(pending, dict(cost_wei="1", cost_period="1"))
    assert item.limits("scope:2:bob", 3601) is None


def test_inclusion_in_new_period_consumes_transaction_allowance(tmp_path):
    item = budget(tmp_path, max_transactions_per_period="1")
    pending = item.reserve("scope:1:alice", 3599, {})
    item.finish(pending, dict(cost_wei="1", cost_period="1"))
    assert item.limits("scope:2:bob", 3601) == "PERIOD_ATTEMPTS"


def test_backoff_and_epoch_attempts_do_not_reset_with_period_or_donation(tmp_path):
    item = budget(tmp_path)
    pair = "scope:1:alice"
    for number, timestamp in enumerate((3599, 7200, 10800)):
        entry = item.reserve(pair, timestamp, dict(donation=str(number)))
        item.finish(entry, dict(cost_wei="0", cost_period=str(timestamp // 3600)))
        if number == 0:
            assert item.limits(pair, 3601) == "BACKOFF"
    assert item.limits(pair, 20000) == "EPOCH_ATTEMPTS"
    assert item.limits("scope:2:alice", 20000) is None


def test_failed_attempt_keeps_counter_but_releases_unsigned_reserve(tmp_path):
    item = budget(tmp_path, max_period_cost_wei="200000")
    first = item.reserve("scope:1:alice", 100, {})
    item.finish(first, dict(cost_wei="0", cost_period="0", status="UNSENT"))
    assert item.limits("scope:1:alice", 101) == "BACKOFF"
    assert item.limits("scope:1:bob", 101) is None


def test_repeated_receipt_settlement_never_double_charges(tmp_path):
    item = budget(tmp_path, max_period_cost_wei="300000")
    first = item.reserve("scope:1:alice", 100, {})
    result = dict(cost_wei="60000", cost_period="0", receipt_status="0")
    item.finish(first, result)
    item.finish(first, result)
    assert item.limits("scope:1:bob", 101) is None


def test_only_one_pending_nonce_can_be_created(tmp_path):
    item = budget(tmp_path)
    first = item.reserve("scope:1:alice", 100, {})
    with pytest.raises(ValueError, match="GAS_CALLER_PENDING"):
        item.reserve("scope:1:bob", 100, {})
    assert budget(tmp_path).pending()["id"] == first["id"]


def test_canonical_clock_rejects_reorg_and_regression(tmp_path):
    item = budget(tmp_path)
    block = SimpleNamespace(number=10, timestamp=100, hash=HexBytes("0x" + "11" * 32))
    w3 = SimpleNamespace(eth=SimpleNamespace(get_block=lambda number: block))
    item.observe(w3, block)
    with pytest.raises(ValueError, match="GAS_CALLER_REORG_RECOVERY_REQUIRED"):
        item.observe(w3, SimpleNamespace(number=9, timestamp=99, hash=block.hash))
    block.hash = HexBytes("0x" + "22" * 32)
    with pytest.raises(ValueError, match="GAS_CALLER_REORG_RECOVERY_REQUIRED"):
        item.observe(w3, block)

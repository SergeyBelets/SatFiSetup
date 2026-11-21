"""Malformed public intents cannot hide independently validated payment plans."""
import copy
import json
from types import SimpleNamespace

import pytest
from hexbytes import HexBytes
from web3.datastructures import AttributeDict

from signer_agent import bitcoin_onchain, bitcoin_onchain_payment
from test_board_intents import indexed_board
from test_bitcoin_onchain_security import public_bitcoin_plan


@pytest.fixture
def pending_board(indexed_board, public_bitcoin_plan, monkeypatch):
    env = indexed_board
    _, env.plan = public_bitcoin_plan
    env.intent = dict(session="7", bitcoin_plan=env.plan)
    env.records.append((7, json.dumps(env.intent).encode()))
    def latest():
        def call(**kwargs):
            env.calls.append(("latestSession", (), kwargs["block_identifier"]))
            return env.records[-1][0] if env.records else 0
        return SimpleNamespace(call=call)
    env.board.functions.latestSession = latest
    monkeypatch.setattr("signer_agent.onchain_transport.board_at", lambda *args: env.board)
    monkeypatch.setattr(bitcoin_onchain, "confirmed_block", lambda *args: env.blocks[10])
    monkeypatch.setattr(bitcoin_onchain_payment, "confirmed_block", lambda *args: env.blocks[10])
    env.args = SimpleNamespace(board=env.board.address, session=None, batch=None, signing_board=None)
    env.registry = SimpleNamespace(w3=env.w3)
    return env


@pytest.mark.parametrize("latest", [
    b"{", b"\xff", b"[]", b"null", b'"text"',
    b'{"padding":"' + b"x" * 32768 + b'"}',
    b'{"nested":' * 80 + b"0" + b"}" * 80,
    b'{"unrelated":"operation"}', b'{"bitcoin_plan":{"version":"invalid"}}',
])
@pytest.mark.parametrize("select_batch", [False, True])
def test_auto_discovery_skips_malformed_latest_and_reuses_index(pending_board, latest, select_batch):
    env = pending_board
    env.records.append((900, latest))
    if select_batch:
        env.args.batch = "0x" + env.plan["batch_id"]
    result = bitcoin_onchain.pending(env.args, env.registry)
    assert result["session"] == "7"
    assert result["intent"] == env.intent
    assert result["plan"] == env.plan
    env.calls.clear()
    assert bitcoin_onchain.pending(env.args, env.registry) == result
    assert env.calls == [("latestSession", (), 10), ("intents", (900,), 10), ("intentCount", (), 10)]


@pytest.mark.parametrize("latest, error", [
    (b"{", "BTC_OPERATION_INTENT_REQUIRED"),
    (b"\xff", "BTC_OPERATION_INTENT_REQUIRED"),
    (b"[]", "BTC_OPERATION_INTENT_REQUIRED"),
    (b"x" * 32769, "BTC_OPERATION_INTENT_REQUIRED"),
    (b'{"nested":' * 80 + b"0" + b"}" * 80, "BTC_OPERATION_INTENT_REQUIRED"),
    (b'{"bitcoin_plan":{"version":"invalid"}}', "BTC_PLAN_SCHEMA"),
    (b'{"unrelated":"operation"}', "BTC_PAYMENT_INTENT_MISSING"),
])
def test_explicit_session_never_substitutes_an_older_valid_intent(pending_board, latest, error):
    env = pending_board
    env.records.append((900, latest))
    env.args.session = 900
    with pytest.raises(ValueError, match=error):
        bitcoin_onchain.pending(env.args, env.registry)
    assert env.calls == [("intents", (900,), 10)]


def test_valid_latest_does_not_read_the_history(pending_board):
    env = pending_board
    assert bitcoin_onchain.pending(env.args, env.registry)["session"] == "7"
    assert env.calls == [("latestSession", (), 10), ("intents", (7,), 10)]


@pytest.mark.parametrize("selector", ["batch", "signing_board"])
def test_selector_mismatch_falls_back_only_to_matching_valid_plan(pending_board, selector):
    env = pending_board
    other = copy.deepcopy(env.plan)
    other["batch_id"] = "ab" * 32
    other["signing_board"] = "0x0000000000000000000000000000000000000009"
    env.records.append((900, json.dumps(dict(session="900", bitcoin_plan=other)).encode()))
    setattr(env.args, selector, env.plan["batch_id" if selector == "batch" else selector])
    assert bitcoin_onchain.pending(env.args, env.registry)["session"] == "7"
    env.args.session = 900
    with pytest.raises(ValueError, match="BTC_PAYMENT_INTENT_MISSING"):
        bitcoin_onchain.pending(env.args, env.registry)


@pytest.mark.parametrize("payload, error", [(b"", ValueError), (None, TypeError),
    (RuntimeError("RPC unavailable"), RuntimeError)])
def test_missing_latest_payload_and_rpc_failure_remain_fatal(pending_board, payload, error):
    env = pending_board
    env.records.append((900, payload))
    with pytest.raises(error):
        bitcoin_onchain.pending(env.args, env.registry)
    assert not any(name == "intentCount" for name, _, _ in env.calls)


@pytest.mark.parametrize("fallback", [False, True])
def test_pending_rejects_reorg_on_both_fast_and_fallback_paths(pending_board, fallback):
    env = pending_board
    if fallback:
        env.records.append((900, b"{"))
    env.w3.eth.get_block = lambda n: AttributeDict(dict(number=n, hash=HexBytes(b"r" * 32)))
    with pytest.raises(ValueError, match="BTC_(BOARD_SNAPSHOT|OPERATION)_REORG"):
        bitcoin_onchain.pending(env.args, env.registry)


def test_empty_board_has_no_pending_operation(pending_board):
    env = pending_board
    env.records.clear()
    with pytest.raises(ValueError, match="BTC_PAYMENT_INTENT_MISSING"):
        bitcoin_onchain.pending(env.args, env.registry)
    assert not any(name == "intents" for name, _, _ in env.calls)


def test_find_session_uses_exact_intent_after_malformed_index_entry(pending_board):
    env = pending_board
    env.records.insert(0, (900, b"{"))
    expected = dict(bitcoin_plan=env.plan)
    assert bitcoin_onchain_payment._find_session(env.registry, env.board, expected)["session"] == "7"
    with pytest.raises(ValueError, match="BTC_OPERATION_PROPOSAL_REQUIRED"):
        bitcoin_onchain_payment._find_session(env.registry, env.board, dict(expected, unauthorized="extra"))

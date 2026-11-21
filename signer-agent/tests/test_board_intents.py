from types import SimpleNamespace
import json
import pytest
from hexbytes import HexBytes
from web3.datastructures import AttributeDict
from signer_agent.board_intents import intent_rows
from signer_agent.secure_state import SecureState
from signer_agent.verified_cache import cache_state
from signer_agent import verified_cache


@pytest.fixture
def indexed_board(tmp_path):
    records, calls = [], []
    blocks = {n: AttributeDict(dict(number=n, hash=HexBytes(bytes([n]) * 32))) for n in (10, 11, 12)}
    def function(name):
        def fn(*args):
            def call(**kwargs):
                calls.append((name, args, kwargs["block_identifier"]))
                if name == "intentCount": return len(records)
                if name == "intentSessions": return records[args[0]][0]
                raw = dict(records)[args[0]]
                if isinstance(raw, Exception): raise raw
                return raw
            return SimpleNamespace(call=call)
        return fn
    board = SimpleNamespace(address="board", functions=SimpleNamespace(**{
        name: function(name) for name in ("intentCount", "intentSessions", "intents")}))
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, get_block=lambda n: blocks[n]))
    token = cache_state.set(SecureState(str(tmp_path / "cache"), "test-cache-key"))
    try:
        yield SimpleNamespace(records=records, calls=calls, blocks=blocks, board=board, w3=w3)
    finally:
        cache_state.reset(token)


@pytest.mark.parametrize("malformed", [
    b"{", b"\xff", b"[]", b"null", b"7", b'"text"',
    b"[" * 10000 + b"]" * 10000,
    b'{"nested":' * 80 + b"0" + b"}" * 80,
    b'{"nested":' * 950 + b"0" + b"}" * 950,
    b'{"padding":"' + b"x" * 32768 + b'"}',
])
def test_malformed_intent_tombstone_preserves_index_and_incremental_cache(indexed_board, malformed):
    env = indexed_board
    env.records.extend([(900, malformed), (7, b'{"operation":"valid"}')])
    rows = intent_rows(env.w3, env.board, env.blocks[10])
    assert [(row["session"], row["intent"]) for row in rows] == [("7", {"operation": "valid"})]
    saved = verified_cache.get(env.w3, "board-intents", [env.board.address])
    assert len(saved["rows"]) == 2
    assert saved["rows"][0] == dict(session="900", intent=None, anchor=dict(number="10", hash=env.blocks[10].hash.hex()))

    env.calls.clear()
    assert intent_rows(env.w3, env.board, env.blocks[11]) == rows
    assert env.calls == [("intentCount", (), 11)]

    env.records.append((4, b'{"operation":"next"}'))
    env.calls.clear()
    assert [row["session"] for row in intent_rows(env.w3, env.board, env.blocks[12])] == ["7", "4"]
    assert env.calls == [("intentCount", (), 12), ("intentSessions", (2,), 12), ("intents", (4,), 12)]


def test_tombstones_still_count_for_index_regression(indexed_board):
    env = indexed_board
    env.records.extend([(900, b"{"), (7, b'{"operation":"valid"}')])
    intent_rows(env.w3, env.board, env.blocks[10])
    env.records.pop()
    with pytest.raises(ValueError, match="BTC_OPERATION_REORG"):
        intent_rows(env.w3, env.board, env.blocks[11])


def test_tombstones_remain_bound_to_canonical_anchor(indexed_board):
    env = indexed_board
    env.records.append((900, b"{"))
    assert intent_rows(env.w3, env.board, env.blocks[10]) == []
    env.blocks[10] = AttributeDict(dict(number=10, hash=HexBytes(b"r" * 32)))
    with pytest.raises(ValueError, match="BTC_OPERATION_REORG"):
        intent_rows(env.w3, env.board, env.blocks[11])


@pytest.mark.parametrize("invalid, expected_error", [
    (b"", ValueError), (None, TypeError), (RuntimeError("RPC unavailable"), RuntimeError),
])
def test_missing_payload_or_rpc_error_never_becomes_tombstone(indexed_board, invalid, expected_error):
    env = indexed_board
    env.records.append((900, b'{"operation":"original"}'))
    intent_rows(env.w3, env.board, env.blocks[10])
    previous = verified_cache.get(env.w3, "board-intents", [env.board.address])
    env.records.extend([(7, b'{"operation":"new"}'), (4, invalid)])
    with pytest.raises(expected_error):
        intent_rows(env.w3, env.board, env.blocks[11])
    # Partial successful reads cannot silently advance the durable cache.
    assert verified_cache.get(env.w3, "board-intents", [env.board.address]) == previous


def test_append_only_index_reuses_authenticated_rows_without_logs(tmp_path):
    records, calls = [(900, {"operation": "one"})], []
    blocks = {n: AttributeDict(dict(number=n, hash=HexBytes(bytes([n]) * 32))) for n in (10, 11, 12)}
    def function(name):
        def fn(*args):
            def call(**kwargs):
                calls.append((name, args, kwargs["block_identifier"]))
                if name == "intentCount": return len(records)
                if name == "intentSessions": return records[args[0]][0]
                return json.dumps(dict(records)[args[0]]).encode()
            return SimpleNamespace(call=call)
        return fn
    board = SimpleNamespace(address="board", functions=SimpleNamespace(**{
        name: function(name) for name in ("intentCount", "intentSessions", "intents")}))
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, get_block=lambda n: blocks[n]))
    token = cache_state.set(SecureState(str(tmp_path / "cache"), "test-cache-key"))
    try:
        assert len(intent_rows(w3, board, blocks[10])) == 1
        calls.clear()
        assert len(intent_rows(w3, board, blocks[11])) == 1
        assert calls == [("intentCount", (), 11)]
        records.append((7, {"operation": "two"}))
        assert [row["session"] for row in intent_rows(w3, board, blocks[12])] == ["900", "7"]
        blocks[12] = AttributeDict(dict(number=12, hash=HexBytes(b"r" * 32)))
        with pytest.raises(ValueError, match="BTC_OPERATION_REORG"):
            intent_rows(w3, board, blocks[12])
    finally:
        cache_state.reset(token)

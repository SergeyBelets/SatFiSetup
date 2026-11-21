from web3.datastructures import AttributeDict
import pytest

from signer_agent.relay_batch import DurableRelayBatch


def test_tip_growth_and_process_restart_replay_same_chunk(tmp_path):
    identity = tmp_path / "identity"
    policy, relay = {"network": "mainnet"}, "relay"
    first, next_chunk = ["aa"*80], ["aa"*80, "bb"*80]
    calls = []
    sender = DurableRelayBatch(identity, 1, relay, policy,
        lambda raws: calls.append(raws) or dict(status="PENDING"))
    assert sender(first)["status"] == "PENDING"
    recovered = DurableRelayBatch(identity, 1, relay, policy,
        lambda raws: calls.append(raws) or AttributeDict({"status": 1}))
    # Finalizing the old chunk cannot pretend it submitted the newly requested chunk.
    assert recovered(next_chunk)["status"] == "PENDING"
    assert calls == [first, first]
    fresh = ["bb"*80]
    assert recovered(fresh)["status"] == 1
    assert calls[-1] == fresh


def test_crash_before_send_retains_exact_chunk(tmp_path):
    calls = []
    def crash(raws):
        calls.append(raws)
        raise RuntimeError("connection dropped")
    original = ["aa"*80]
    sender = DurableRelayBatch(tmp_path / "id", 1, "relay", {}, crash)
    with pytest.raises(RuntimeError):
        sender(original)
    recovered = DurableRelayBatch(tmp_path / "id", 1, "relay", {},
        lambda raws: calls.append(raws) or dict(status="VERIFIED"))
    recovered(["bb"*80])
    assert calls == [original, original]


def test_policy_cannot_change_inside_same_lane(tmp_path):
    first = DurableRelayBatch(tmp_path / "id", 1, "relay", {"network": "mainnet"}, lambda _: dict(status="PENDING"))
    first(["aa"*80])
    second = DurableRelayBatch(tmp_path / "id", 1, "relay", {"network": "testnet4"}, lambda _: pytest.fail("sent with changed policy"))
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        second(["aa"*80])


def test_chain_and_relay_have_independent_pending_lanes(tmp_path):
    calls = []
    def sender(raws):
        calls.append(raws)
        return dict(status="PENDING")
    for chain, relay, byte in [(1, "a", "aa"), (11155111, "a", "bb"), (1, "b", "cc")]:
        DurableRelayBatch(tmp_path / "id", chain, relay, {}, sender)([byte*80])
    assert calls == [["aa"*80], ["bb"*80], ["cc"*80]]


@pytest.fixture
def confirmed_relay(monkeypatch):
    from types import SimpleNamespace
    from signer_agent.relay_batch import _headers_confirmed
    from test_bitcoin_network import Core, Relay, policy, raw, CHECKPOINT
    core, config = Core(), policy()
    relay = Relay(core, config)
    block = SimpleNamespace(number=77, hash=b"b" * 32)
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=1,
        contract=lambda **kw: relay, get_block=lambda number: block))
    monkeypatch.setattr("signer_agent.evm_network.read_block", lambda *args: block)
    monkeypatch.setattr("signer_agent.bitcoin_trust.checked_runtime", lambda *args: None)
    headers = [raw(CHECKPOINT + 1).hex(), raw(CHECKPOINT + 2).hex()]
    for value in headers:
        relay.add(value)
    def reconcile():
        return _headers_confirmed(w3, "0x" + "11" * 20, "MainnetHeaderRelay", config, 12, headers)
    return SimpleNamespace(w3=w3, relay=relay, block=block, headers=headers, reconcile=reconcile)


def test_unsigned_reconcile_requires_every_header_in_one_confirmed_snapshot(confirmed_relay):
    f = confirmed_relay
    assert f.reconcile() is True
    from signer_agent.bitcoin_network import header_hash
    f.relay.head = header_hash(bytes.fromhex(f.headers[0]))
    del f.relay.stored[header_hash(bytes.fromhex(f.headers[1]))]
    assert f.reconcile() is False


@pytest.mark.parametrize("field", [0, 1, 2, 3, 4, 5])
def test_unsigned_reconcile_rejects_forged_stored_header_or_work(confirmed_relay, field):
    from signer_agent.bitcoin_network import header_hash
    f = confirmed_relay
    digest = header_hash(bytes.fromhex(f.headers[0]))
    record = list(f.relay.stored[digest])
    record[field] = b"x" * 32 if field < 2 else record[field] + 1
    f.relay.stored[digest] = record
    with pytest.raises(ValueError, match="^MAINNET_RELAY_WORK_MISMATCH$"):
        f.reconcile()


def test_unsigned_reconcile_rejects_snapshot_reorg_and_latest_halt(confirmed_relay):
    from types import SimpleNamespace
    f = confirmed_relay
    f.w3.eth.get_block = lambda _: SimpleNamespace(hash=b"x" * 32)
    with pytest.raises(ValueError, match="^BTC_REGISTRY_READ_REORG$"):
        f.reconcile()
    f.relay.halted = True
    with pytest.raises(ValueError, match="^MAINNET_RELAY_HALTED$"):
        f.reconcile()


@pytest.mark.parametrize("confirmations", [True, 12.5, "012", "12.0", None])
def test_canonical_sender_does_not_coerce_ambiguous_finality_policy(tmp_path, confirmations):
    from types import SimpleNamespace
    with pytest.raises(ValueError, match="^EVM_CONFIRMATIONS_POLICY$"):
        DurableRelayBatch.for_rpc(tmp_path / "id", SimpleNamespace(), "unused", {}, confirmations)


def test_pending_recovery_does_not_need_new_headers_and_retries_same_chunk(tmp_path):
    calls = []
    first = DurableRelayBatch(tmp_path / "id", 1, "relay", {},
        lambda headers: calls.append(headers) or dict(status="BROADCAST"))
    assert first.recover_pending() is None
    original = ["aa" * 80]
    assert first(original)["status"] == "BROADCAST"
    second = DurableRelayBatch(tmp_path / "id", 1, "relay", {},
        lambda headers: calls.append(headers) or dict(status="VERIFIED"))
    assert second.recover_pending()["status"] == "VERIFIED"
    assert second.recover_pending() is None
    assert calls == [original, original]

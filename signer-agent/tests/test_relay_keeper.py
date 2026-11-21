"""Keeper recovery is retrying the same durable sender, never signing a new lane."""
from types import SimpleNamespace
from unittest.mock import Mock
import json
from pathlib import Path

import pytest
import rlp
from requests.exceptions import ConnectionError, HTTPError
from web3 import Web3
from web3.datastructures import AttributeDict

from signer_agent import relay_keeper as keeper
from signer_agent.contract_provenance import creation_input
from signer_agent.verified_cache import cache_state


class Stop:
    def __init__(self, after=100):
        self.delays, self.after = [], after

    def is_set(self):
        return len(self.delays) >= self.after

    def wait(self, delay):
        self.delays.append(delay)


def drive(tick, *, once=False, timeout=0, stop=None, monotonic=lambda: 0):
    output = []
    result = keeper.run(SimpleNamespace(tick=tick), interval=10, max_backoff=30,
        once=once, timeout=timeout, stop=stop or Stop(4), emit=output.append, monotonic=monotonic)
    return result, output


def test_transient_rpc_retries_same_keeper_without_dkg_or_new_sender():
    tick = Mock(side_effect=[ConnectionError("https://user:secret@rpc.invalid/key"),
        RuntimeError("BITCOIN_RPC_FAILED"), dict(status="IN_PROGRESS", reason="EVM_HEADER_PENDING"),
        dict(status="MAINNET_RELAY_READY", lag="0")])
    stop = Stop(4)
    result, output = drive(tick, stop=stop)
    assert result == 0 and tick.call_count == 4
    assert stop.delays == [20, 30, 10, 10]
    assert [row["status"] for row in output] == ["WAITING", "WAITING", "IN_PROGRESS", "MAINNET_RELAY_READY", "STOPPED"]
    assert "secret" not in str(output) and "https" not in str(output)


@pytest.mark.parametrize("code", ["MAINNET_RELAY_HALTED", "MAINNET_RELAY_REORG_LIMIT",
    "EVM_REORG_RECOVERY_REQUIRED", "RELAY_RUNTIME_PIN_MISMATCH", "MAINNET_RELAY_POLICY_MISMATCH"])
def test_consensus_pin_and_reorg_failures_stop_for_review(code):
    tick = Mock(side_effect=ValueError(code))
    result, output = drive(tick)
    assert result == 78 and tick.call_count == 1
    assert output == [dict(status="STOPPED", code=code, retryable=False)]


@pytest.mark.parametrize("status,expected", [("MAINNET_RELAY_READY", 0), ("IN_PROGRESS", 2)])
def test_once_is_suitable_for_health_check_or_cron(status, expected):
    tick = Mock(return_value=dict(status=status))
    result, _ = drive(tick, once=True)
    assert result == expected and tick.call_count == 1


def test_timeout_preserves_pending_and_reports_non_success():
    moments = iter([0, 2])
    result, output = drive(lambda: dict(status="IN_PROGRESS"), timeout=1, monotonic=lambda: next(moments))
    assert result == 2 and output[-1]["status"] == "TIMEOUT"


def test_http_rate_limit_is_retryable_without_credential_text():
    response = SimpleNamespace(status_code=429)
    assert keeper.error_code(HTTPError("API_KEY", response=response)) == "EVM_RPC_UNAVAILABLE"
    assert keeper.error_code(ValueError("SECRET_CREDENTIAL")) == "ValueError"


def test_core_warmup_retries_but_other_json_rpc_rejections_stop_with_numeric_code():
    tick = Mock(side_effect=[keeper.BitcoinRPCError("BITCOIN_RPC_REJECTED", -28),
        keeper.BitcoinRPCError("BITCOIN_RPC_REJECTED", -8)])
    result, output = drive(tick)
    assert result == 78
    assert output == [dict(status="WAITING", code="BITCOIN_RPC_WARMUP", retryable=True, rpc_error_code=-28),
        dict(status="STOPPED", code="BITCOIN_RPC_REJECTED", retryable=False, rpc_error_code=-8)]


def test_core_auth_error_does_not_retry_forever():
    result, output = drive(Mock(side_effect=keeper.BitcoinRPCError("BITCOIN_RPC_FAILED", http_status=401)))
    assert result == 78 and output == [dict(status="STOPPED", code="BITCOIN_RPC_AUTH_REJECTED",
        retryable=False, http_status=401)]


def test_tick_reuses_origin_proof_and_shared_durable_sender_but_checks_pin_each_time(monkeypatch, tmp_path):
    block = SimpleNamespace(number=25, hash=b"a" * 32)
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=11155111, get_block=lambda _: block))
    relay = SimpleNamespace(address="relay")
    args = SimpleNamespace(chain_id=11155111, evm_confirmations=12, relay_code_hash="11" * 32,
        bitcoin_rpc="http://127.0.0.1:18332", bitcoin_cookie="cookie", identity_file="gas-only-key")
    worker = object.__new__(keeper.RelayKeeper)
    worker.args, worker.w3, worker.relay = args, w3, relay
    worker.policy, worker.origin_anchor, worker.sender = {"network": "testnet4"}, None, None
    worker.provenance_state = None
    monkeypatch.setattr(keeper, "read_block", lambda *args: block)
    runtime = Mock(return_value="11" * 32)
    monkeypatch.setattr(keeper, "checked_runtime", runtime)
    monkeypatch.setattr(keeper, "relay_constructor_args", lambda _: [])
    proof = Mock()
    monkeypatch.setattr(keeper, "prove_creation", proof)
    core = Mock()
    monkeypatch.setattr(keeper, "BitcoinRPC", core)
    durable = Mock()
    monkeypatch.setattr(keeper.DurableRelayBatch, "for_rpc", durable)
    sync = Mock(return_value={"status": "MAINNET_RELAY_READY"})
    monkeypatch.setattr(keeper, "sync_relay", sync)
    worker.tick()
    worker.tick()
    assert runtime.call_count == 2 and core.call_count == 2  # includes cookie rotation
    assert proof.call_count == 1 and durable.call_count == 1
    assert all(call.kwargs["send_headers"] is durable.return_value for call in sync.call_args_list)
    args.relay_code_hash = "22" * 32
    with pytest.raises(ValueError, match="RELAY_RUNTIME_PIN_MISMATCH"):
        worker.tick()
    assert sync.call_count == 2
    args.relay_code_hash = "11" * 32
    args.bitcoin_cookie = str(tmp_path / "cookie")
    core.side_effect = ValueError("BITCOIN_RPC_COOKIE_INVALID")
    with pytest.raises(ValueError, match="BITCOIN_RPC_COOKIE_UNAVAILABLE"):
        worker.tick()
    (tmp_path / "cookie").write_text("invalid cookie")
    with pytest.raises(ValueError, match="BITCOIN_RPC_COOKIE_INVALID"):
        worker.tick()
    assert sync.call_count == 2


class RelayHistory:
    """Actual compiled relay constructor/runtime with synthetic public receipts."""

    def __init__(self, policy):
        self.policy, self.birth, self.latest = policy, 8, 25
        sender = Web3.to_checksum_address("0x" + "09" * 20)
        self.address = Web3.to_checksum_address(Web3.keccak(rlp.encode([
            bytes.fromhex(sender[2:]), 0]))[-20:])
        self.runtime = bytes.fromhex(keeper.artifact("MainnetHeaderRelay")["deployedBytecode"]["object"].removeprefix("0x"))
        self.blocks = {number: AttributeDict(dict(number=number,
            hash=Web3.keccak(text="keeper block %d" % number), transactions=[]))
            for number in range(self.latest + 1)}
        self.tx = dict(hash=Web3.keccak(text="keeper relay deployment"), to=None,
            input=creation_input("MainnetHeaderRelay", keeper.relay_constructor_args(policy)),
            nonce=0, **{"from": sender})
        self.blocks[self.birth]["transactions"].append(self.tx)
        self.receipt = dict(blockNumber=self.birth, blockHash=self.blocks[self.birth].hash,
            transactionHash=self.tx["hash"], status=1, contractAddress=self.address, logs=[])
        self.code_calls, self.receipt_calls, self.full_block_calls = [], [], []
        self.recent_only = False

    def provider(self):
        # A new provider deliberately has no in-memory _satoshify_creation_proofs.
        return SimpleNamespace(codec=Web3().codec, eth=SimpleNamespace(chain_id=11155111,
            get_code=self.get_code, get_block=self.get_block,
            get_transaction_receipt=self.get_receipt))

    def get_code(self, address, block_identifier):
        self.code_calls.append(block_identifier)
        if self.recent_only:
            assert block_identifier == self.latest, "archive state must not be queried"
        assert address == self.address
        return self.runtime if block_identifier >= self.birth else b""

    def get_block(self, number, full_transactions=False):
        if full_transactions:
            self.full_block_calls.append(number)
        return self.blocks[number]

    def get_receipt(self, tx_hash):
        self.receipt_calls.append(tx_hash)
        assert tx_hash == self.tx["hash"]
        return self.receipt


@pytest.fixture
def provenance_keeper(monkeypatch, tmp_path):
    from signer_agent.bitcoin_network import MAINNET_GENESIS

    fixtures = Path(__file__).resolve().parents[2] / "web3/test/fixtures/mainnet"
    headers = (fixtures / "headers-32246-36288.bin").read_bytes()
    records = (fixtures / "expected-32246-36288.bin").read_bytes()
    header = lambda height: headers[(height - 32246) * 80:(height - 32245) * 80].hex()
    policy = dict(version="1", network="mainnet", genesis_hash=MAINNET_GENESIS,
        checkpoint_header=header(32256), checkpoint_height="32256",
        checkpoint_chainwork=records[10 * 68 + 32:10 * 68 + 64].hex(),
        checkpoint_predecessors=[header(height) for height in range(32246, 32256)],
        max_reorg_depth="6", max_tip_age_seconds="86400", max_relay_lag_blocks="0",
        max_headers_per_sync="2")
    policy_path, identity = tmp_path / "bitcoin-policy.json", tmp_path / "gas-identity.json"
    policy_path.write_text(json.dumps(policy))
    private_key = "0x" + "71" * 32  # Public deterministic test identity, never funded.
    identity.write_text(json.dumps(dict(private_key=private_key)))
    identity.chmod(0o600)
    history = RelayHistory(policy)
    args = SimpleNamespace(rpc="http://127.0.0.1:1", bitcoin_policy=str(policy_path),
        relay=history.address, chain_id=11155111, evm_confirmations=12,
        relay_code_hash=Web3.keccak(history.runtime).hex(), relay_creation_block=None,
        bitcoin_rpc="http://127.0.0.1:2", bitcoin_cookie=str(tmp_path / "cookie"),
        identity_file=str(identity))
    monkeypatch.setattr(keeper, "read_block", lambda w3, _: w3.eth.get_block(history.latest))
    monkeypatch.setattr(keeper, "BitcoinRPC", Mock())
    monkeypatch.setattr(keeper.DurableRelayBatch, "for_rpc", Mock())
    sync = Mock(return_value=dict(status="MAINNET_RELAY_READY"))
    monkeypatch.setattr(keeper, "sync_relay", sync)

    def fresh():
        worker = keeper.RelayKeeper(args)
        worker.w3 = history.provider()
        return worker

    return SimpleNamespace(history=history, args=args, fresh=fresh, sync=sync,
        identity=identity, private_key=private_key, policy_path=policy_path,
        cache_path=Path(str(identity) + ".relay-provenance.enc"))


def test_keeper_restart_uses_authenticated_public_proof_without_archive_state(provenance_keeper):
    env = provenance_keeper
    first = env.fresh()
    assert first.tick()["status"] == "MAINNET_RELAY_READY"
    assert env.history.receipt_calls == [env.history.tx["hash"]]
    assert any(number < env.history.latest for number in env.history.code_calls)
    encrypted = env.cache_path.read_bytes()
    assert env.private_key.encode() not in encrypted
    assert bytes.fromhex(env.private_key[2:]) not in encrypted
    assert b"private_key" not in encrypted
    assert env.cache_path.stat().st_mode & 0o777 == 0o600
    public = json.dumps(first.provenance_state._load())
    assert "private_key" not in public and env.private_key[2:] not in public
    env.history.recent_only = True
    env.history.code_calls.clear()
    env.history.full_block_calls.clear()
    env.history.receipt_calls.clear()
    restarted = env.fresh()
    assert restarted.tick()["status"] == "MAINNET_RELAY_READY"
    assert env.history.code_calls and set(env.history.code_calls) == {env.history.latest}
    assert env.history.full_block_calls == [] and env.history.receipt_calls == []
    assert cache_state.get() is None


def test_keeper_cache_scope_restored_on_success_and_rpc_failure(provenance_keeper):
    env = provenance_keeper
    worker = env.fresh()
    sentinel = object()
    token = cache_state.set(sentinel)
    try:
        worker.tick()
        assert cache_state.get() is sentinel
        env.sync.side_effect = ConnectionError("private RPC endpoint")
        with pytest.raises(ConnectionError):
            worker.tick()
        assert cache_state.get() is sentinel
    finally:
        cache_state.reset(token)


def test_keeper_restart_rejects_tampered_cache_without_rpc_send(provenance_keeper):
    env = provenance_keeper
    env.fresh().tick()
    encrypted = bytearray(env.cache_path.read_bytes())
    encrypted[len(encrypted) // 2] ^= 1
    env.cache_path.write_bytes(encrypted)
    result, output = drive(env.fresh().tick, once=True)
    assert result == 78 and env.sync.call_count == 1
    assert env.private_key[2:] not in str(output)
    assert cache_state.get() is None


def test_keeper_cache_is_bound_to_gas_identity(provenance_keeper):
    env = provenance_keeper
    env.fresh().tick()
    env.identity.write_text(json.dumps(dict(private_key="0x" + "72" * 32)))
    with pytest.raises(RuntimeError, match="cannot decrypt signer state"):
        env.fresh().tick()
    assert env.sync.call_count == 1 and cache_state.get() is None


@pytest.mark.parametrize("change, expected", [
    ("reorg", "BTC_CREATION_ANCHOR_REORG"),
    ("runtime", "BTC_CONTRACT_BUILD_MISMATCH"),
    ("pin", "RELAY_RUNTIME_PIN_MISMATCH"),
    ("policy", "BTC_CONSTRUCTOR_INPUT_MISMATCH"),
])
def test_keeper_restart_revalidates_origin_runtime_and_policy(provenance_keeper, change, expected):
    env = provenance_keeper
    env.fresh().tick()
    if change == "reorg":
        original = dict(env.history.blocks[env.history.birth])
        env.history.blocks[env.history.birth] = AttributeDict(dict(original,
            hash=Web3.keccak(text="replacement deployment block")))
    elif change == "runtime":
        env.history.runtime = env.history.runtime[:-1]
    elif change == "pin":
        env.args.relay_code_hash = "22" * 32
    else:
        policy = json.loads(env.policy_path.read_text())
        policy["max_reorg_depth"] = "7"
        env.policy_path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match=expected):
        env.fresh().tick()
    assert env.sync.call_count == 1 and cache_state.get() is None


def test_keeper_each_tick_rechecks_live_runtime_and_observation_anchor(provenance_keeper):
    env = provenance_keeper
    worker = env.fresh()
    worker.tick()
    original = dict(env.history.blocks[env.history.latest])
    env.history.blocks[env.history.latest] = AttributeDict(dict(original,
        hash=Web3.keccak(text="replacement confirmed observation")))
    with pytest.raises(ValueError, match="EVM_REORG_RECOVERY_REQUIRED"):
        worker.tick()
    assert env.sync.call_count == 1


def test_keeper_creation_block_hint_needs_no_archive_state_but_still_checks_receipt(provenance_keeper):
    env = provenance_keeper
    env.args.relay_creation_block = env.history.birth
    env.history.recent_only = True
    assert env.fresh().tick()["status"] == "MAINNET_RELAY_READY"
    assert env.history.receipt_calls == [env.history.tx["hash"]]
    assert set(env.history.code_calls) == {env.history.latest}


def test_keeper_wrong_creation_block_hint_cannot_authorize_relay(provenance_keeper):
    env = provenance_keeper
    env.args.relay_creation_block = env.history.birth + 1
    with pytest.raises(ValueError, match="BTC_CANONICAL_CREATION_NOT_FOUND"):
        env.fresh().tick()
    assert env.sync.call_count == 0 and not env.cache_path.exists()


def test_keeper_cache_never_accepts_identity_symlink(provenance_keeper, tmp_path):
    env = provenance_keeper
    link = tmp_path / "identity-link.json"
    link.symlink_to(env.identity)
    env.args.identity_file = str(link)
    with pytest.raises(OSError):
        env.fresh()
    assert not env.cache_path.exists()

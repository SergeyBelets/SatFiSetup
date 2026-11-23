"""Live-policy safety tests using pinned historical data; no production RPC/funds.

Mocks explicitly supply a historical clock. They are not a live mainnet readiness
claim; a real historical/header-only Core is tested to fail the live requirements.
"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from signer_agent.bitcoin_network import (
    MAINNET_GENESIS, check_core, checkpoint_policy, header_hash, inspect_relay,
    relay_constructor_args, sync_relay, validate_policy,
)


FIXTURE = Path(__file__).resolve().parents[2] / "web3/test/fixtures/mainnet"
FIRST, CHECKPOINT = 32246, 32256
HEADERS = (FIXTURE / "headers-32246-36288.bin").read_bytes()
RECORDS = (FIXTURE / "expected-32246-36288.bin").read_bytes()


def raw(height):
    offset = (height - FIRST) * 80
    return HEADERS[offset:offset + 80]


def record(height):
    offset = (height - FIRST) * 68
    return RECORDS[offset:offset + 68]


def policy(**overrides):
    result = dict(version="1", network="mainnet", genesis_hash=MAINNET_GENESIS,
                  checkpoint_header=raw(CHECKPOINT).hex(), checkpoint_height=str(CHECKPOINT),
                  checkpoint_chainwork=record(CHECKPOINT)[32:64].hex(),
                  checkpoint_predecessors=[raw(h).hex() for h in range(CHECKPOINT - 10, CHECKPOINT)],
                  max_reorg_depth="6", max_tip_age_seconds="86400", max_relay_lag_blocks="0",
                  max_headers_per_sync="2")
    return dict(result, **overrides)


class Core:
    def __init__(self, height=CHECKPOINT + 10):
        self.height = height
        self.info_changes = {}
        self.index = {"txindex": {"synced": True, "best_block_height": height}}
        self.hashes = {record(h)[:32].hex(): h for h in range(FIRST, height + 1)}
        self.genesis = MAINNET_GENESIS
        self.mutate_header = lambda item: item
        self.calls = []

    @property
    def now(self):
        return int.from_bytes(raw(self.height)[68:72], "little") + 10

    def call(self, method, *args):
        self.calls.append((method, args))
        if method == "getblockchaininfo":
            return dict(dict(chain="main", blocks=self.height, headers=self.height,
                             bestblockhash=record(self.height)[:32].hex(),
                             chainwork=record(self.height)[32:64].hex(),
                             time=self.now - 10, initialblockdownload=False, pruned=False), **self.info_changes)
        if method == "getindexinfo":
            return self.index
        if method == "getblockhash":
            return self.genesis if args[0] == 0 else record(args[0])[:32].hex()
        if method == "getblockheader":
            height = self.hashes[args[0]]
            if args[1] is False:
                return raw(height).hex()
            return self.mutate_header(dict(hash=args[0], height=height, confirmations=self.height - height + 1,
                                          time=int.from_bytes(raw(height)[68:72], "little"),
                                          bits=record(height)[64:].hex(), chainwork=record(height)[32:64].hex()))
        pytest.fail("Unexpected RPC method: " + method)


class Relay:
    def __init__(self, core, configured=None):
        self.core = core
        self.policy = configured or policy()
        self.head = header_hash(raw(CHECKPOINT))
        self.height_map = {CHECKPOINT: self.head}
        self.stored = {self.head: self.data(CHECKPOINT)}
        self.halted = False
        self.test_helpers = False
        self.w3 = SimpleNamespace(eth=SimpleNamespace(
            chain_id=31337, block_number=100, get_block=lambda number: SimpleNamespace(hash=bytes([number]) * 32)))
        outer = self
        class Functions:
            def __getattr__(self, name): return outer.__getattr__(name)
        self.functions = Functions()

    @staticmethod
    def data(height):
        header = raw(height)
        return (header[4:36], header[36:68], int.from_bytes(record(height)[32:64], "big"),
                height, int.from_bytes(header[68:72], "little"), int.from_bytes(header[72:76], "little"))

    def __getattr__(self, name):
        def make(*args):
            def call(**kwargs):
                if name == "testHelpersEnabled": return self.test_helpers
                if name == "halted": return self.halted
                if name == "checkpointHash": return header_hash(bytes.fromhex(self.policy["checkpoint_header"]))
                if name == "checkpointHeight": return int(self.policy["checkpoint_height"])
                if name == "checkpointChainWork": return int(self.policy["checkpoint_chainwork"], 16)
                if name == "maxReorgDepth": return int(self.policy["max_reorg_depth"])
                if name == "getMainchainHead": return self.head
                if name == "getMainchainHeight": return self.stored[self.head][3]
                if name == "height2hash": return self.height_map.get(args[0], bytes(32))
                if name == "blocks": return self.stored.get(args[0], (bytes(32), bytes(32), 0, 0, 0, 0))
                pytest.fail("Unexpected contract call: " + name)
            return SimpleNamespace(call=call)
        return make

    def add(self, value):
        digest = header_hash(bytes.fromhex(value))
        height = self.core.hashes[digest[::-1].hex()]
        self.stored[digest] = self.data(height)
        if self.stored[digest][2] > self.stored[self.head][2]:
            self.head = digest
            self.height_map.update({h: header_hash(raw(h)) for h in range(CHECKPOINT, height + 1)})


def test_policy_constructor_uses_raw_hash_order_and_native_abi_types():
    original = policy()
    checked = validate_policy(original)
    checked["checkpoint_predecessors"][0] = "00"
    assert original["checkpoint_predecessors"][0] != "00"
    args = relay_constructor_args(original)
    assert args[0] == raw(CHECKPOINT) and args[1] == CHECKPOINT
    assert args[2] == int.from_bytes(record(CHECKPOINT)[32:64], "big")
    assert args[3] == [raw(h) for h in range(CHECKPOINT - 10, CHECKPOINT)] and args[4] == 6


@pytest.mark.parametrize("name,value", [
    ("version", 1), ("network", "regtest"), ("genesis_hash", "00" * 32),
    ("checkpoint_height", "32257"), ("checkpoint_height", "032256"),
    ("checkpoint_height", 32256), ("checkpoint_chainwork", "00" * 32),
    ("checkpoint_header", "00" * 80), ("checkpoint_header", raw(CHECKPOINT).hex().upper()),
    ("checkpoint_predecessors", []), ("checkpoint_predecessors", [raw(CHECKPOINT).hex()] * 10),
    ("max_reorg_depth", "0"), ("max_reorg_depth", "145"),
    ("max_tip_age_seconds", "0"), ("max_tip_age_seconds", "86401"),
    ("max_relay_lag_blocks", "145"), ("max_relay_lag_blocks", "-1"),
    ("max_headers_per_sync", "0"), ("max_headers_per_sync", "65"),
])
def test_policy_rejects_ambiguous_unsafe_or_nonmainnet_configuration(name, value):
    with pytest.raises(ValueError): validate_policy(policy(**{name: value}))


@pytest.mark.parametrize("change", [lambda p: p.update(secret="never permitted"), lambda p: p.pop("checkpoint_chainwork")])
def test_policy_requires_exact_fields(change):
    p = policy(); change(p)
    with pytest.raises(ValueError, match="MAINNET_POLICY_FIELDS"): validate_policy(p)


def test_strict_live_core_policy_accepts_mock_consistent_snapshot_and_rejects_real_current_time():
    core = Core()
    assert check_core(core, "mainnet", policy(), now=core.now)["chain"] == "main"
    with pytest.raises(ValueError, match="MAINNET_TIP_STALE"):
        check_core(core, "mainnet", policy())


@pytest.mark.parametrize("change,error", [
    ({"chain": "regtest"}, "BITCOIN_NETWORK_MISMATCH"),
    ({"chain": "test"}, "BITCOIN_NETWORK_MISMATCH"),
    ({"initialblockdownload": True}, "MAINNET_CORE_NOT_SYNCED"),
    ({"initialblockdownload": 0}, "MAINNET_CORE_NOT_SYNCED"),
    ({"headers": CHECKPOINT + 11}, "MAINNET_CORE_NOT_SYNCED"),
    ({"pruned": True}, "MAINNET_CORE_PRUNED"),
    ({"pruned": 0}, "MAINNET_CORE_PRUNED"),
    ({"chainwork": "00" * 32}, "MAINNET_CORE_TIP_MISMATCH"),
    ({"bestblockhash": "00" * 32}, "MAINNET_CORE_TIP_MISMATCH"),
])
def test_live_core_guards(change, error):
    core = Core(); core.info_changes.update(change)
    with pytest.raises(ValueError, match=error): check_core(core, "mainnet", policy(), now=core.now)


@pytest.mark.parametrize("index", [{}, {"txindex": {"synced": False, "best_block_height": CHECKPOINT + 10}},
                                    {"txindex": {"synced": True, "best_block_height": CHECKPOINT + 9}}])
def test_live_core_requires_complete_transaction_index(index):
    core = Core(); core.index = index
    with pytest.raises(ValueError): check_core(core, "mainnet", policy(), now=core.now)


def test_core_genesis_and_checkpoint_chainwork_are_trust_anchors():
    core = Core(); core.genesis = "11" * 32
    with pytest.raises(ValueError, match="MAINNET_GENESIS_MISMATCH"): check_core(core, "mainnet", policy(), now=core.now)
    core.genesis = MAINNET_GENESIS
    forged = policy(checkpoint_chainwork="f" * 64)
    with pytest.raises(ValueError, match="MAINNET_CHECKPOINT_MISMATCH"): check_core(core, "mainnet", forged, now=core.now)


def test_checkpoint_must_be_deeper_than_reviewed_reorg_limit():
    core = Core(height=CHECKPOINT + 5)
    with pytest.raises(ValueError, match="MAINNET_CHECKPOINT_TOO_RECENT"): check_core(core, "mainnet", policy(), now=core.now)


@pytest.mark.parametrize("change", [{"height": CHECKPOINT + 1}, {"hash": "11" * 32}, {"confirmations": -1}, {"bits": "1d00ffff"}, {"time": 0}])
def test_core_header_rpc_fields_must_agree_with_hashed_bytes(change):
    core = Core(); core.mutate_header = lambda h: dict(h, **change)
    with pytest.raises(ValueError): check_core(core, "mainnet", policy(), now=core.now)


def test_core_snapshot_reorg_is_not_accepted():
    core = Core(); original = core.call; reads = []
    def call(method, *args):
        if method == "getblockhash" and args == (core.height,):
            reads.append(True)
            if len(reads) > 1: return "11" * 32
        return original(method, *args)
    core.call = call
    with pytest.raises(ValueError, match="MAINNET_CORE_REORG_DURING_CHECK"):
        check_core(core, "mainnet", policy(), now=core.now)


def test_core_retries_one_inconsistent_header_count(monkeypatch):
    core = Core()
    original = core.call
    reads = []

    def call(method, *args):
        result = original(method, *args)
        if method == "getblockchaininfo":
            reads.append(method)
            if len(reads) == 1:
                return dict(result, headers=result["blocks"] + 1)
        return result

    core.call = call
    monkeypatch.setattr("signer_agent.bitcoin_network.time.sleep", lambda _: None)
    assert check_core(core, "mainnet", policy(), now=core.now)["chain"] == "main"
    assert len(reads) == 2


def test_network_is_explicit_and_regtest_cannot_take_mainnet_policy():
    core = Core()
    with pytest.raises(ValueError, match="MAINNET_POLICY_FIELDS"): check_core(core, "mainnet")
    with pytest.raises(ValueError, match="REGTEST_POLICY_FORBIDDEN"): check_core(core, "regtest", policy())
    with pytest.raises(ValueError, match="BITCOIN_NETWORK_UNSUPPORTED"): check_core(core, "testnet")
    core.info_changes["chain"] = "regtest"
    assert check_core(core)["chain"] == "regtest"


def test_relay_sync_is_bounded_resumable_and_idempotent():
    core = Core(); p = policy(); relay = Relay(core, p); published = []
    def send(value): published.append(value); relay.add(value)
    first = sync_relay(core, relay, p, send, now=core.now)
    assert first["status"] == "IN_PROGRESS" and first["submitted"] == "2"
    for _ in range(4): result = sync_relay(core, relay, p, send, now=core.now)
    assert result["status"] == "MAINNET_RELAY_READY" and len(published) == 10
    result = sync_relay(core, relay, p, send, now=core.now)
    assert result["status"] == "MAINNET_RELAY_READY" and result["submitted"] == "0"
    assert len(published) == 10


def test_pending_evm_header_stops_before_next_submission():
    core = Core(); relay = Relay(core); published = []
    result = sync_relay(core, relay, policy(), published.append, now=core.now)
    assert result == dict(status="IN_PROGRESS", submitted="1", reason="EVM_HEADER_PENDING")
    assert len(published) == 1


def test_partial_reorg_longer_than_batch_progresses_without_duplicate_sends():
    core = Core(); p = policy(max_headers_per_sync="1"); relay = Relay(core, p)
    for height in range(CHECKPOINT + 1, CHECKPOINT + 4):
        old_hash = bytes([height % 256]) * 32
        relay.stored[old_hash] = relay.data(height)
        relay.height_map[height] = old_hash
        relay.head = old_hash
    published = []
    def send(value): published.append(value); relay.add(value)
    for _ in range(10): result = sync_relay(core, relay, p, send, now=core.now)
    assert result["status"] == "MAINNET_RELAY_READY"
    assert len(published) == len(set(published)) == 10


def test_halted_or_too_deep_relay_never_sends_headers():
    core = Core(); relay = Relay(core); relay.halted = True
    with pytest.raises(ValueError, match="MAINNET_RELAY_HALTED"):
        sync_relay(core, relay, policy(), lambda value: pytest.fail("must not send"), now=core.now)
    relay.halted = False
    for height in range(CHECKPOINT + 1, CHECKPOINT + 8):
        old_hash = bytes([height % 256]) * 32
        relay.stored[old_hash] = relay.data(height); relay.height_map[height] = old_hash; relay.head = old_hash
    with pytest.raises(ValueError, match="MAINNET_RELAY_REORG_LIMIT"):
        sync_relay(core, relay, policy(), lambda value: pytest.fail("must not send"), now=core.now)


def test_halt_event_after_successful_header_transaction_is_failure():
    core = Core(); relay = Relay(core)
    def send(value): relay.halted = True
    with pytest.raises(ValueError, match="MAINNET_RELAY_HALTED"):
        sync_relay(core, relay, policy(), send, now=core.now)


def test_relay_inspection_requires_fresh_canonical_work_and_policy():
    core = Core(); relay = Relay(core)
    with pytest.raises(ValueError, match="MAINNET_RELAY_STALE"): inspect_relay(core, relay, policy(), now=core.now)
    assert inspect_relay(core, relay, policy(), require_fresh=False, now=core.now)["height"] == str(CHECKPOINT)
    for height in range(CHECKPOINT + 1, core.height + 1): relay.add(raw(height).hex())
    assert inspect_relay(core, relay, policy(), now=core.now)["status"] == "MAINNET_RELAY_READY"
    relay.policy = policy(max_reorg_depth="5")
    with pytest.raises(ValueError, match="MAINNET_RELAY_POLICY_MISMATCH"): inspect_relay(core, relay, policy(), now=core.now)
    relay.policy = policy(); fields = list(relay.stored[relay.head]); fields[2] += 1; relay.stored[relay.head] = tuple(fields)
    with pytest.raises(ValueError, match="MAINNET_RELAY_WORK_MISMATCH"): inspect_relay(core, relay, policy(), now=core.now)


def test_checkpoint_candidate_has_no_hidden_parameters_and_rechecks_live_core(monkeypatch):
    core = Core(); monkeypatch.setattr("signer_agent.bitcoin_network.time.time", lambda: core.now)
    fields = {key: policy()[key] for key in ("checkpoint_height", "max_reorg_depth", "max_tip_age_seconds", "max_relay_lag_blocks", "max_headers_per_sync")}
    assert checkpoint_policy(core, **fields) == policy()
    core.info_changes["initialblockdownload"] = True
    with pytest.raises(ValueError, match="MAINNET_CORE_NOT_SYNCED"): checkpoint_policy(core, **fields)


@pytest.mark.parametrize("args", [
    ["proof", "--bitcoin-network", "mainnet", "--state", "unused"],
    ["checkpoint", "--bitcoin-network", "mainnet"],
    ["proof", "--bitcoin-policy", "must-not-read.json", "--state", "unused"],
])
def test_cli_rejects_missing_or_conflicting_policy_before_rpc(monkeypatch, args):
    from signer_agent import bitcoin_cli
    monkeypatch.setattr(bitcoin_cli, "BitcoinRPC", lambda *a, **k: pytest.fail("must not open RPC"))
    with pytest.raises(SystemExit) as exc:
        bitcoin_cli.main([*args, "--bitcoin-rpc", "http://127.0.0.1:8332", "--bitcoin-cookie", "unused"])
    assert exc.value.code == 2


def test_cli_checkpoint_outputs_exact_review_candidate_without_files_or_transactions(monkeypatch, capsys):
    from signer_agent import bitcoin_cli
    core = Core(); created = []
    monkeypatch.setattr("signer_agent.bitcoin_network.time.time", lambda: core.now)
    def rpc(url, cookie, **options):
        created.append(options)
        return core
    monkeypatch.setattr(bitcoin_cli, "BitcoinRPC", rpc)
    result = bitcoin_cli.main([
        "checkpoint", "--bitcoin-network", "mainnet", "--bitcoin-rpc", "http://localhost:8332",
        "--bitcoin-cookie", "local-only", "--checkpoint-height", str(CHECKPOINT),
        "--max-reorg-depth", "6", "--max-tip-age-seconds", "86400",
        "--max-relay-lag-blocks", "0", "--max-headers-per-sync", "2",
    ])
    assert result == 0 and created == [{"network": "mainnet"}]
    assert json.loads(capsys.readouterr().out) == policy()
    assert all(method not in ("sendrawtransaction", "submitheader") for method, _ in core.calls)


def test_cli_mainnet_relay_uses_shared_durable_sender_and_reports_in_progress(monkeypatch, tmp_path, capsys):
    from signer_agent import bitcoin_cli
    core = Core(); relay = Relay(core); relay.address = "0x" + "11" * 20; p = policy(); batches = []
    identity = str(tmp_path / "private-identity")
    relay.address = "0x" + "11" * 20
    monkeypatch.setattr("signer_agent.bitcoin_network.time.time", lambda: core.now)
    core.check_network = lambda: check_core(core, "mainnet", p, now=core.now)
    monkeypatch.setattr(bitcoin_cli, "BitcoinRPC", lambda *a, **k: core)
    monkeypatch.setattr(bitcoin_cli, "_checked_mainnet_relay", lambda *a: relay)
    monkeypatch.setattr(bitcoin_cli, "evm_web3", lambda _: SimpleNamespace(eth=SimpleNamespace(chain_id=11155111)))
    def durable_sender(actual_identity, w3, address, actual_policy, depth):
        assert actual_identity == identity and address == relay.address
        assert actual_policy == p and depth == 1 and w3.eth.chain_id == 11155111
        def send(headers):
            batches.append(headers)
            for raw in headers:
                relay.add(raw)
            return {"status": "VERIFIED"}
        return send
    monkeypatch.setattr("signer_agent.relay_batch.DurableRelayBatch.for_rpc", durable_sender)
    policy_file = tmp_path / "public-policy.json"; policy_file.write_text(json.dumps(p))
    bitcoin_cli.main([
        "relay", "--bitcoin-network", "mainnet", "--bitcoin-policy", str(policy_file),
        "--bitcoin-rpc", "http://localhost:8332", "--bitcoin-cookie", "local-only",
        "--state", str(tmp_path / "journal.sqlite"), "--rpc", "http://localhost:8545",
        "--chain-id", "11155111", "--evm-confirmations", "1", "--relay", "0x" + "11" * 20, "--identity-file", str(tmp_path / "private-identity"),
    ])
    assert json.loads(capsys.readouterr().out)["status"] == "IN_PROGRESS"
    assert batches == [[raw(CHECKPOINT + index).hex() for index in (1, 2)]]


def test_pending_confirmation_does_not_count_as_fresh_even_when_header_is_visible():
    core = Core(height=CHECKPOINT + 6); relay = Relay(core)
    def send(value):
        relay.add(value)
        return {"status": "AWAITING_CONFIRMATIONS"}
    result = sync_relay(core, relay, policy(), send, now=core.now, evm_confirmations=3)
    assert result == dict(status="IN_PROGRESS", submitted="1", reason="EVM_HEADER_PENDING")


def test_evm_confirmed_snapshot_is_used_and_reorg_is_rejected():
    core = Core(); relay = Relay(core)
    reads = []
    def get_block(number):
        reads.append(number)
        return SimpleNamespace(hash=bytes([number + (1 if len(reads) > 1 else 0)]) * 32)
    relay.w3.eth.get_block = get_block
    with pytest.raises(ValueError, match="BTC_REGISTRY_READ_REORG"):
        sync_relay(core, relay, policy(), lambda _: pytest.fail("must not send"), now=core.now, evm_confirmations=3)
    assert reads == [98, 98]


@pytest.mark.parametrize("confirmations", [0, -1, True, "3", 2**32])
def test_sync_rejects_invalid_evm_confirmation_counts(confirmations):
    core = Core(); relay = Relay(core)
    with pytest.raises(ValueError, match="MAINNET_EVM_CONFIRMATIONS"):
        sync_relay(core, relay, policy(), lambda _: pytest.fail("must not send"), now=core.now, evm_confirmations=confirmations)


def test_shorter_heavier_core_branch_can_replace_old_relay_within_rollback_bound():
    # Synthetic relay state tests branch selection only. Actual mainnet header PoW
    # and Core cumulative work still use the unmodified historical fixture bytes.
    core = Core(height=CHECKPOINT + 10); relay = Relay(core); p = policy(max_headers_per_sync="4")
    for height in range(CHECKPOINT + 1, CHECKPOINT + 7): relay.add(raw(height).hex())
    top_work = int.from_bytes(record(core.height)[32:64], "big")
    for height in range(CHECKPOINT + 7, CHECKPOINT + 13):
        old_hash = bytes([height % 256]) * 32
        fields = list(relay.data(height)); fields[2] = top_work - (CHECKPOINT + 13 - height)
        relay.stored[old_hash] = tuple(fields); relay.height_map[height] = old_hash; relay.head = old_hash
    assert relay.stored[relay.head][3] > core.height
    result = sync_relay(core, relay, p, relay.add, now=core.now)
    assert result["status"] == "MAINNET_RELAY_READY" and result["height"] == str(core.height)
    assert relay.head == header_hash(raw(core.height))


@pytest.fixture
def lowlevel_settlement(monkeypatch):
    from signer_agent import bitcoin_cli
    import signer_agent.bitcoin_trust as trust
    import signer_agent.contract_provenance as provenance
    calls = []; proofs = []; codes = []
    escrow_address, batch_address, guard_address, relay_address = ['0x' + value * 40 for value in ('1', '2', '3', '4')]
    proof = dict(raw='00', txid='11' * 32, block_hash='22' * 32, siblings=['33' * 32], index='0', confirmations='6')
    class Contract:
        def __init__(self, address, values):
            self.address = address; self.values = values
            class Functions:
                def __getattr__(_, method):
                    def invoke(*args):
                        def call(**kw):
                            calls.append((address, method, kw.get('block_identifier')))
                            return self.values[method]
                        return SimpleNamespace(call=call)
                    return invoke
            self.functions = Functions()
    escrow = Contract(escrow_address, dict(depth=6, minP2WPKHSats=294, minP2TRSats=330, batches=batch_address, spv=guard_address))
    guard = Contract(guard_address, dict(relay=relay_address, maxTipAgeSeconds=86400, isFresh=True,
                                        getBlockStatus=(True, 6), checkTxInclusion=True))
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, block_number=200, get_block=lambda number: SimpleNamespace(number=200 if number == "latest" else number, hash=bytes([200 if number == "latest" else number]) * 32),
                                            contract=lambda **kw: guard))
    bitcoin = SimpleNamespace(call=lambda method, *args: {'height': 42000} if method == 'getblockheader' else '22' * 32)
    monkeypatch.setattr(bitcoin_cli, 'artifact', lambda name: {'abi': []})
    monkeypatch.setattr(bitcoin_cli, 'inclusion', lambda *args: dict(proof))
    monkeypatch.setattr(bitcoin_cli, '_checked_mainnet_relay', lambda *a, **kw: calls.append(('relay', 'provenance', kw['block'].number)) or object())
    monkeypatch.setattr(bitcoin_cli, 'inspect_relay', lambda *a, **kw: calls.append(('relay', 'inspect', kw['block_identifier'])))
    monkeypatch.setattr(trust, 'checked_runtime', lambda w3, address, kind, block: codes.append((address, kind, block)))
    monkeypatch.setattr(provenance, 'prove_creation', lambda w3, address, kind, args, block: proofs.append((address, kind, args, block)))
    return SimpleNamespace(w3=w3, bitcoin=bitcoin, escrow=escrow, guard=guard, proof=proof, calls=calls, proofs=proofs, codes=codes)


def test_low_level_mainnet_settlement_uses_guard_and_exact_escrow_constructor_at_confirmed_snapshot(lowlevel_settlement):
    from signer_agent.bitcoin_cli import _mainnet_settlement_observation
    env = lowlevel_settlement
    assert _mainnet_settlement_observation(env.w3, env.bitcoin, env.escrow, policy(), '3', '11' * 32) == env.proof
    assert all(block == 198 for _, _, block in env.calls)
    escrow_proof = env.proofs[0]
    assert escrow_proof == (env.escrow.address, 'BitcoinWithdrawalEscrow', ['0x' + '2' * 40, env.guard.address, 6, 294, 330], 198)
    assert (env.guard.address, 'getBlockStatus', 198) in env.calls
    assert (env.guard.address, 'checkTxInclusion', 198) in env.calls
    assert (env.escrow.address, 'BitcoinWithdrawalEscrow', 198) in env.codes


@pytest.mark.parametrize('field,value,error', [('isFresh', False, 'MAINNET_RELAY_STALE'),
    ('getBlockStatus', (False, 0), 'PAYMENT_DEPTH'), ('getBlockStatus', (True, 5), 'PAYMENT_DEPTH'),
    ('checkTxInclusion', False, 'INVALID_PAYMENT_PROOF')])
def test_low_level_mainnet_settlement_cannot_bypass_guard(lowlevel_settlement, field, value, error):
    from signer_agent.bitcoin_cli import _mainnet_settlement_observation
    env = lowlevel_settlement; env.guard.values[field] = value
    with pytest.raises(ValueError, match=error):
        _mainnet_settlement_observation(env.w3, env.bitcoin, env.escrow, policy(), '3', '11' * 32)


@pytest.mark.parametrize('reorg', [False, True])
def test_low_level_mainnet_settlement_refreshes_proof_before_burn(monkeypatch, tmp_path, capsys, reorg):
    from signer_agent import bitcoin_cli
    observations = []; applied = []
    proof = dict(raw='00', txid='11' * 32, block_hash='22' * 32, siblings=[], index='0', confirmations='6')
    def observation(*args):
        observations.append(True)
        return dict(proof, confirmations=str(5 + len(observations)),
                    block_hash='33' * 32 if reorg and len(observations) == 2 else proof['block_hash'])
    monkeypatch.setattr(bitcoin_cli, '_mainnet_settlement_observation', observation)
    monkeypatch.setattr(bitcoin_cli, 'BitcoinRPC', lambda *a, **kw: SimpleNamespace(check_network=lambda: None))
    monkeypatch.setattr(bitcoin_cli, 'artifact', lambda name: {'abi': []})
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=11155111, contract=lambda **kw: object()))
    monkeypatch.setattr(bitcoin_cli, 'evm_web3', lambda _: w3)
    class Durable:
        def __init__(self, profile, state): pass
        def apply(self, identity): applied.append(True); return {'status': 'VERIFIED'}
    monkeypatch.setattr(bitcoin_cli, 'Bootstrap', Durable)
    filename = tmp_path / 'policy.json'; filename.write_text(json.dumps(policy()))
    args = ['settle', '--bitcoin-network', 'mainnet', '--bitcoin-policy', str(filename),
            '--bitcoin-rpc', 'http://localhost:8332', '--bitcoin-cookie', 'local.cookie',
            '--rpc', 'http://localhost:8545', '--chain-id', '11155111', '--evm-confirmations', '3',
            '--escrow', '0x' + '11' * 20, '--batch', '44' * 32, '--txid', '11' * 32,
            '--identity-file', 'local.json', '--state', str(tmp_path / 'journal.sqlite')]
    if reorg:
        with pytest.raises(ValueError, match='BITCOIN_PAYMENT_REORG'): bitcoin_cli.main(args)
        assert not applied
    else:
        assert bitcoin_cli.main(args) == 0 and applied == [True]
    assert len(observations) == 2

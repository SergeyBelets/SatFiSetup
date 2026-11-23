"""Local inbox, budgets, native witness construction and worker recovery."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace as NS

import pytest
from web3 import Web3
from signer_agent import zk_worker
from signer_agent.zk_worker import Inbox, Worker, load_config, state_wire
from signer_agent.zk_spv import VERIFIER_SELECTOR
from signer_agent.zk_worker_prover import ROOT, LocalProver, read, sha, write
from test_zk_bootstrap import zk_invite


def config(tmp_path):
    binary = Path(os.environ.get("SATOHSI_ZK_WORKER_HOST", str(ROOT / "ops/.local-tools/zk-worker/satoshifi-zk-worker-host")))
    return dict(version="1", chain_id="31337", evm_rpc="http://127.0.0.1:1", evm_confirmations="1",
        bitcoin_rpc="http://127.0.0.1:2", bitcoin_cookie=str(tmp_path / "cookie"), bitcoin_policy=zk_invite()["bitcoin_policy"],
        zk_state=Web3.to_checksum_address("0x5FbDB2315678afecb367f032d93F642f64180aa3"), identity_file=str(tmp_path / "identity.json"),
        directory=str(tmp_path / "worker"), allowed_consumers={Web3.to_checksum_address("0x"+"22"*20): "MiningPoolV1"},
        host_binary=str(binary), host_sha256=sha(binary) if binary.exists() else "00"*32,
        stage_timeout_seconds="7200", max_stage_retries="2", max_pending="8", max_attempts="8", minimum_free_bytes=str(8*1024**3),
        max_tx_gas="2000000", max_transaction_cost_wei=str(10**17), max_total_cost_wei=str(10**18), poll_seconds="5")


def test_config_scope_permissions_and_budget_ranges(tmp_path):
    c = config(tmp_path); path = tmp_path / "config.json"; write(path, c); path.chmod(0o600)
    assert load_config(path) == c
    for key, value in [("chain_id", "2"), ("stage_timeout_seconds", "0"), ("max_tx_gas", "1"),
                       ("directory", "relative"), ("allowed_consumers", {}), ("host_sha256", "xx")]:
        write(path, dict(c, **{key: value}))
        with pytest.raises(ValueError): load_config(path)
    write(path, c); path.chmod(0o666)
    with pytest.raises(ValueError, match="PERMISSIONS"): load_config(path)


def test_retained_needs_limit_validates_the_history_bound(tmp_path):
    c = config(tmp_path)
    path = tmp_path / "worker.json"
    for retained in ("8", "4096"):
        configured = dict(c, max_retained_needs=retained)
        write(path, configured)
        assert load_config(path) == configured
    for retained in (None, 8, "0", "7", "08", "4097"):
        write(path, dict(c, max_retained_needs=retained))
        with pytest.raises(ValueError, match="^ZK_WORKER_CONFIG_RANGE:max_retained_needs$"):
            load_config(path)


def test_witness_is_private_configuration_and_lag_waits_after_pin(tmp_path, monkeypatch):
    from test_evm_rpc import node

    cfg = dict(config(tmp_path), evm_rpc="https://primary.example.test/secret",
               evm_witness_rpc="https://witness.example.test/secret",
               chain_id="11155111", evm_confirmations="4")
    path = tmp_path / "worker.json"
    write(path, cfg)
    assert load_config(path) == cfg
    with pytest.raises(ValueError, match="^EVM_RPC_HTTPS_REQUIRED$"):
        write(path, dict(cfg, evm_rpc="http://external.example.test/secret"))
        load_config(path)
    review_fields = ("version", "chain_id", "evm_rpc", "evm_confirmations", "evm_witness_rpc",
                     "bitcoin_policy", "zk_state")
    review = {key: cfg[key] for key in review_fields}
    write(path, review)
    assert load_config(path, review=True) == review
    worker = Worker.__new__(Worker)
    worker.c, worker.inbox = cfg, Inbox(cfg)
    worker.w3 = node(120)
    worker.evm_witness = node(115)
    worker.bitcoin = object()
    state = (None,) * 8 + (False,)
    primary_values = [state]
    contract = NS(functions=NS(halted=lambda: NS(call=lambda: False),
                               getState=lambda: NS(call=lambda **_: primary_values[0]),
                               isFresh=lambda: NS(call=lambda **_: True)))
    witness = NS(functions=NS(halted=lambda: NS(call=lambda: False),
                              getState=lambda: NS(call=lambda **_: state),
                              isFresh=lambda: NS(call=lambda **_: True)))
    monkeypatch.setattr(zk_worker, "inspect_binding", lambda w3, *a:
                        (contract if w3 is worker.w3 else witness, None, {}, None))
    monkeypatch.setattr(zk_worker, "check_core", lambda *a: None)
    try:
        first, witnessed_state, _, _, _ = worker.context()
        assert first.number == 112
        assert worker.inbox.get("evm-witness-height") == 112
        assert "secret" not in json.dumps(worker.inbox.get("scope"))
        primary_values[0] = (False,) * 9
        assert witnessed_state.functions.getState().call(block_identifier=112) == state
        with pytest.raises(ValueError, match="^EVM_RPC_WITNESS_MISMATCH$"):
            worker.context()
        primary_values[0] = state
        worker.evm_witness = node(114)
        with pytest.raises(ValueError, match="^EVM_RPC_BEHIND$") as reason:
            worker.context()
        assert zk_worker.worker_failure(reason.value)["status"] == "AWAITING_EVM_RPC"
        worker.evm_witness = node(115, fork=112)
        with pytest.raises(ValueError, match="^EVM_RPC_WITNESS_MISMATCH$"):
            worker.context()
        assert worker.inbox.get("evm-witness-height") == 112
        worker.evm_witness = node(116)
        assert worker.context()[0].number == 113
    finally:
        worker.inbox.close()


def test_cpu_batch_limit_timeout_and_local_cuda_pin_are_bounded(tmp_path):
    c = dict(config(tmp_path), headers_per_attempt="4", stage_timeout_seconds="86400",
             cuda=dict(r0vm_binary=str(tmp_path / "r0vm-cuda"), r0vm_sha256="11" * 32, segment_limit_po2="18"))
    path = tmp_path / "config.json"
    write(path, c)
    assert load_config(path) == c
    for field, value in (("headers_per_attempt", "9999"), ("stage_timeout_seconds", "86401"),
                         ("cuda", dict(c["cuda"], segment_limit_po2="25"))):
        write(path, dict(c, **{field: value}))
        with pytest.raises(ValueError):
            load_config(path)


def test_durable_dedup_scope_and_shared_budget(tmp_path):
    c = config(tmp_path); c.update(max_pending="1", max_transaction_cost_wei="10", max_total_cost_wei="15")
    a = Inbox(c); b = Inbox(c)
    need = dict(kind="deposit", consumer=next(iter(c["allowed_consumers"])), txid="11"*32, vout="0")
    key = a.enqueue(need); assert b.enqueue(need) == key
    with pytest.raises(ValueError, match="INBOX_FULL"): b.enqueue(dict(need, vout="1"))
    a.reserve("one", 8); b.reserve("one", 8); b.reserve("two", 7)
    with pytest.raises(ValueError, match="GAS_BUDGET"): a.reserve("three", 1)
    with pytest.raises(ValueError, match="GAS_BUDGET"): a.reserve("one", 9)
    a.close(); b.close()
    c["zk_state"] = Web3.to_checksum_address("0x"+"33"*20)
    with pytest.raises(ValueError, match="SCOPE_CHANGED"): Inbox(c)


def test_completed_needs_release_pending_capacity_with_retention_limit(tmp_path):
    c = dict(config(tmp_path), max_pending="1", max_transaction_cost_wei="8", max_total_cost_wei="8")
    need = dict(kind="deposit", consumer=next(iter(c["allowed_consumers"])), txid="11" * 32, vout="0")
    later = dict(need, vout="1")
    completed = dict(need, vout="2")
    blocked = dict(need, vout="3")

    legacy = Inbox(c)
    key = legacy.enqueue(need, observation_status="COMPLETED")
    legacy.reserve("first", 8)
    with pytest.raises(ValueError, match="^ZK_WORKER_INBOX_FULL$"):
        legacy.enqueue(later)
    legacy.close()

    inbox = Inbox(dict(c, max_retained_needs="3"))
    try:
        assert inbox.enqueue(need) == key
        assert inbox.needs() == [(key, need, "COMPLETED")]
        later_key = inbox.enqueue(later, observation_status="READY")
        with pytest.raises(ValueError, match="^ZK_WORKER_INBOX_FULL$"):
            inbox.enqueue(blocked)

        # Canonical observations are free to reopen a previous need at capacity.
        assert inbox.enqueue(need, observation_status="NEEDS_STATE") == key
        inbox.status(later_key, "COMPLETED")
        with pytest.raises(ValueError, match="^ZK_WORKER_INBOX_FULL$"):
            inbox.enqueue(blocked)
        completed_key = inbox.enqueue(completed, observation_status="COMPLETED")
        assert inbox.needs() == [(key, need, "NEEDS_STATE"), (later_key, later, "COMPLETED"),
                                 (completed_key, completed, "COMPLETED")]
        inbox.status(key, "COMPLETED")
        with pytest.raises(ValueError, match="^ZK_WORKER_INBOX_FULL$"):
            inbox.enqueue(blocked)
        assert inbox.enqueue(later) == later_key
        with pytest.raises(ValueError, match="^ZK_WORKER_GAS_BUDGET$"):
            inbox.reserve("later", 1)
    finally:
        inbox.close()


def test_archived_needs_keep_ids_and_review_reorg_in_pages(tmp_path):
    cfg = dict(config(tmp_path), max_pending="1", max_retained_needs="1", history_max_bytes=str(64 * 2**20),
               max_transaction_cost_wei="8", max_total_cost_wei="8")
    first = NS(number=100, hash=b"a" * 32)
    recent = NS(number=100, hash=b"b" * 32)
    older = NS(number=99, hash=b"o" * 32)
    worker = Worker.__new__(Worker)
    worker.c = cfg
    worker.inbox = Inbox(cfg)
    seen = []
    worker.w3 = NS(eth=NS(get_block=lambda number: recent))
    worker.stable = lambda block: None

    def observe(key, need, *_):
        seen.append(key)
        return dict(status="NEEDS_STATE" if need == {"index": 0} else "COMPLETED")

    worker.observe_queued_need = observe
    keys = []
    try:
        for i in range(40):
            key = worker.inbox.enqueue({"index": i}, observation_status="COMPLETED")
            keys.append(key)
            worker.inbox.checkpoint_completed([key], first)
        worker.inbox.put("service:" + keys[0], {"service_job_id": "old-paid-job", "deadline": "42"})
        worker.inbox.reserve("original", 8)
        assert worker.inbox.needs(active_only=True) == []
        assert worker.inbox.enqueue({"index": 0}) == keys[0]
        assert worker._history_current(first)
        with pytest.raises(ValueError, match="^EVM_RPC_BEHIND$"):
            worker._history_current(older)
        assert not worker._history_current(recent)
        assert worker._recheck_history(recent, None, {}) == dict(status="ZK_NEED_HISTORY_RECHECK", checked=32)
        worker.inbox.close()
        worker.inbox = Inbox(cfg)
        assert not worker._history_current(recent)
        assert worker._recheck_history(recent, None, {}) == dict(status="ZK_NEED_HISTORY_RECHECK", checked=8)
        assert worker._recheck_history(recent, None, {}) == dict(status="ZK_NEED_HISTORY_RECHECKED", checked=0)
        assert len(seen) == len(set(seen)) == 40
        assert worker._history_current(recent)
        assert worker.inbox.needs(active_only=True) == [(keys[0], {"index": 0}, "NEEDS_STATE")]
        assert worker.inbox.get("service:" + keys[0])["deadline"] == "42"
        with pytest.raises(ValueError, match="^ZK_WORKER_INBOX_FULL$"):
            worker.inbox.enqueue({"index": 40})
        with pytest.raises(ValueError, match="^ZK_WORKER_GAS_BUDGET$"):
            worker.inbox.reserve("new", 1)
        assert worker.inbox.history_status()["archived"] == 39
    finally:
        worker.inbox.close()
    cfg.pop("history_max_bytes")
    with pytest.raises(ValueError, match="^SPV_HISTORY_POLICY_REQUIRED$"):
        Inbox(cfg)


def fixture_headers():
    data = (ROOT / "web3/test/fixtures/mainnet/headers-32246-36288.bin").read_bytes()
    return {height: data[(height-32246)*80:(height-32245)*80] for height in range(32256, 32265)}


def make_worker(tmp_path):
    c = config(tmp_path)
    if not Path(c["host_binary"]).exists():
        if os.environ.get("SATOHSI_REQUIRE_NATIVE") == "1":pytest.fail("Build pinned worker host first: " + c["host_binary"])
        pytest.skip("Build pinned worker host first")
    w = Worker.__new__(Worker); w.c = c; w.inbox = Inbox(c); w.lock_fd = None
    return w


def test_native_exact_guest_input_archive_and_membership(tmp_path):
    w = make_worker(tmp_path); headers = fixture_headers()
    first = tmp_path / "first"
    result = w.native([], [headers[32257].hex()], [32257], first)
    reference = ROOT / "rust/zkspv/state-proof-integration/evidence/local-state"
    assert (first / "state.input").read_bytes() == (reference / "state.input").read_bytes()
    assert (first / "state.journal").read_bytes() == (reference / "state.journal").read_bytes()
    assert result["after"][3] == "32257" and len(result["memberships"][0]["siblings"]) == 64
    second = w.native(result["archive"], [headers[32258].hex()], [32257], tmp_path / "second")
    assert second["before"] == result["after"] and second["after"][3] == "32258"
    assert second["memberships"][0]["siblings"] != result["memberships"][0]["siblings"]
    assert second["after"][9] != result["after"][9]
    w.inbox.close()


def fake_state(native):
    v = native
    state = [bytes.fromhex(x) if i in (0, 2, 6, 7, 9, 10) else x if i == 8
             else int(x, 16) if i == 4 else int(x) for i, x in enumerate(v)]
    return state


def simulated(w, monkeypatch, tmp_path):
    """Synthetic observation/transaction boundaries; not cryptographic evidence."""
    from signer_agent.bitcoin_network import header_hash
    headers = fixture_headers()
    initial = w.native([], [], [], tmp_path / "initial")["before"]
    model = dict(state=initial, fresh=False, materialized=False, sends=[], proves=0)
    def call(fn): return NS(call=lambda **kw: fn())
    state = NS(address=w.c["zk_state"], functions=NS(getState=lambda: call(lambda: fake_state(model["state"])),
               isFresh=lambda: call(lambda: model["fresh"]), halted=lambda: call(lambda: model["state"][8])))
    w.context = lambda **kw: (NS(number=1, hash=bytes(32)), state, None, {}, dict(blocks=32257))
    w.stable = lambda b: None
    w.aligned = lambda *a: True
    def rpc(method, *args):
        if method == "getblockhash": return header_hash(headers[args[0]])[::-1].hex()
        if method == "getblockheader":
            height = next(h for h, raw in headers.items() if header_hash(raw)[::-1].hex() == args[0])
            return dict(height=height) if args[1] else headers[height].hex()
        raise AssertionError(method)
    w.bitcoin = NS(call=rpc)
    # Depth 1 is confined to this diagnostic unit simulation. The real admission
    # method enforces consumer >=6 / coinbase=100 and is tested separately.
    w.observe_need = lambda *a: dict(status="READY" if model["materialized"] else "NEEDS_STATE",
        height=32257, depth=1, header=headers[32257].hex())
    def prove(self, base): model["proves"] += 1; return VERIFIER_SELECTOR + bytes(256)
    monkeypatch.setattr(LocalProver, "prove", prove)
    def send(name, step, records):
        model["sends"].append(step["method"])
        if step["method"] == "submitTransition":
            a = w.inbox.get("attempt"); n = read(w.inbox.path / "attempts" / a["id"] / "native.json")
            model.update(state=n["after"], fresh=True)
        else: model["materialized"] = True
        return dict(status="VERIFIED")
    w.publish = send
    return model


def test_worker_prepare_restart_publish_materialize_and_no_duplicate(tmp_path, monkeypatch):
    w = make_worker(tmp_path); m = simulated(w, monkeypatch, tmp_path)
    request = dict(kind="deposit", consumer=next(iter(w.c["allowed_consumers"])), txid="11"*32, vout="0")
    key = w.inbox.enqueue(request)
    assert w.tick()["status"] == "PROOF_PREPARED"
    attempt = w.inbox.get("attempt")
    w.inbox.close(); w.inbox = Inbox(w.c)  # crash/restart after atomic preparation
    assert w.inbox.get("attempt") == attempt
    assert w.tick()["status"] == "STATE_PROGRESS"
    assert w.tick()["status"] == "HEADER_PROGRESS"
    assert w.tick()["status"] == "IDLE"
    assert w.inbox.enqueue(request) == key and w.tick()["status"] == "IDLE"
    assert m["proves"] == 1 and m["sends"] == ["submitTransition", "materializeHeader"]
    assert w.inbox.needs()[0][2] == "READY"
    w.inbox.close()


def test_prepare_service_without_publish_identity_or_prover(tmp_path, monkeypatch, capsys):
    from signer_agent import zk_service_issuer, zk_worker_service, zk_worker_transport

    worker = make_worker(tmp_path)
    model = simulated(worker, monkeypatch, tmp_path)
    worker.c.update(service={"mode": "confederation"}, transport={"enabled": True}, automation={"enabled": True})
    worker.inbox.enqueue(dict(kind="deposit", consumer=next(iter(worker.c["allowed_consumers"])),
                              txid="33" * 32, vout="0"))

    def forbidden(*args, **kwargs):
        pytest.fail("Prepare-only must leave proving, service authorization and publication off")

    monkeypatch.setattr(zk_worker, "load_config", lambda path: worker.c)
    monkeypatch.setattr(zk_worker, "Worker", lambda config: worker)
    monkeypatch.setattr(zk_worker, "LocalProver", forbidden)
    monkeypatch.setattr(zk_worker_service, "preflight", forbidden)
    monkeypatch.setattr(zk_worker_service, "reserve", forbidden)
    monkeypatch.setattr(zk_worker_transport, "WorkerTransport", forbidden)
    monkeypatch.setattr(zk_service_issuer, "tick", forbidden)
    worker.publish = forbidden
    monkeypatch.setattr(worker, "close", lambda: None)

    assert zk_worker.main(["run", "--config", "config.json", "--boundary", "prepare", "--once"]) == 0
    attempt = worker.inbox.get("attempt")
    assert json.loads(capsys.readouterr().out) == dict(status="PROOF_PREPARED", proving_attempt_id=attempt["id"])
    base = worker.inbox.path / "attempts" / attempt["id"]
    assert (base / "state.input").is_file() and not (base / "local-result").exists()
    assert not Path(worker.c["identity_file"]).exists()

    native = read(base / "native.json")
    model.update(state=native["after"], fresh=True)
    assert worker.advance("prepare")["status"] == "STATE_ALREADY_ACCEPTED"
    assert worker.inbox.get("attempt") == attempt
    assert worker.prepare_service()["status"] == "STATE_ALREADY_ACCEPTED"
    assert worker.inbox.get("attempt") is None
    assert worker.inbox.get("archive:" + native["after"][7]) == native["archive"]
    assert worker.prepare_service() == {"status": "MATERIALIZATION_REQUIRED"}
    assert worker.inbox.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
    assert not (worker.inbox.path / "membership").exists()
    worker.inbox.close()


def test_prepare_service_keeps_publication_recovery_ownership(tmp_path, monkeypatch):
    worker = make_worker(tmp_path)
    model = simulated(worker, monkeypatch, tmp_path)
    worker.inbox.enqueue(dict(kind="deposit", consumer=next(iter(worker.c["allowed_consumers"])),
                              txid="44" * 32, vout="0"))
    assert worker.prepare_service()["status"] == "PROOF_PREPARED"
    active = dict(worker.inbox.get("attempt"), phase="SENDING")
    worker.inbox.put("attempt", active)
    assert worker.prepare_service()["status"] == "PUBLICATION_RECOVERY_REQUIRED"

    native = read(worker.inbox.path / "attempts" / active["id"] / "native.json")
    model.update(state=native["after"], fresh=True)
    assert worker.prepare_service()["status"] == "PUBLICATION_RECOVERY_REQUIRED"
    assert worker.inbox.get("attempt") == active
    assert worker.inbox.get("archive:" + native["after"][7]) is None
    assert model["proves"] == 0 and model["sends"] == []
    worker.inbox.close()


def test_prepare_service_retires_stale_state_and_keeps_lagging_evm_attempt(tmp_path, monkeypatch):
    worker = make_worker(tmp_path)
    model = simulated(worker, monkeypatch, tmp_path)
    worker.inbox.enqueue(dict(kind="deposit", consumer=next(iter(worker.c["allowed_consumers"])),
                              txid="45" * 32, vout="0"))
    assert worker.prepare_service()["status"] == "PROOF_PREPARED"
    active = worker.inbox.get("attempt")
    base = worker.inbox.path / "attempts" / active["id"]
    original = worker.context()
    worker.context = lambda **kwargs: (NS(number=0, hash=bytes(32)), *original[1:])
    assert worker.prepare_service()["status"] == "AWAITING_EVM_RPC"
    assert worker.inbox.get("attempt") == active

    worker.context = lambda **kwargs: original
    other = read(base / "native.json")["after"]
    other[7] = "55" * 32
    model["state"] = other
    assert worker.advance("prepare")["status"] == "REVALIDATE_STATE"
    assert worker.inbox.get("attempt") == active
    assert worker.prepare_service() == dict(status="REVALIDATE_STATE", proving_attempt_id=active["id"], retired=True)
    assert worker.inbox.get("attempt") is None
    assert (base / "preparation.json").is_file()
    with pytest.raises(ValueError, match="^ZK_ARCHIVE_RECOVERY_REQUIRED$"):
        worker.prepare_service()
    assert model["proves"] == 0 and model["sends"] == []
    worker.inbox.close()


def test_prepare_service_retires_stale_bitcoin_only_after_consistent_observation(tmp_path, monkeypatch):
    worker = make_worker(tmp_path)
    model = simulated(worker, monkeypatch, tmp_path)
    worker.inbox.enqueue(dict(kind="deposit", consumer=next(iter(worker.c["allowed_consumers"])),
                              txid="46" * 32, vout="0"))
    assert worker.prepare_service()["status"] == "PROOF_PREPARED"
    active = worker.inbox.get("attempt")
    base = worker.inbox.path / "attempts" / active["id"]
    original = worker.context()
    new_hash = "66" * 32
    worker.context = lambda **kwargs: (*original[:-1], dict(blocks=active["target_height"] - 1))
    assert worker.prepare_service()["status"] == "WAITING_BITCOIN_RPC"
    assert worker.inbox.get("attempt") == active

    worker.context = lambda **kwargs: (*original[:-1], dict(blocks=active["target_height"], bestblockhash=active["target_hash"]))
    observations = iter([new_hash, active["target_hash"]])
    worker.bitcoin.call = lambda *args: next(observations)
    assert worker.prepare_service()["status"] == "WAITING_BITCOIN_RPC"
    assert worker.inbox.get("attempt") == active

    worker.context = lambda **kwargs: (*original[:-1], dict(blocks=active["target_height"], bestblockhash=new_hash))
    worker.bitcoin.call = lambda *args: new_hash
    assert worker.advance("prepare")["status"] == "REVALIDATE_BITCOIN"
    assert worker.inbox.get("attempt") == active
    assert worker.prepare_service() == dict(status="REVALIDATE_BITCOIN", proving_attempt_id=active["id"], retired=True)
    assert worker.inbox.get("attempt") is None
    assert (base / "preparation.json").is_file()
    assert model["proves"] == 0 and model["sends"] == []
    worker.inbox.close()


def test_worker_bundle_cli_export_is_one_shot_and_verification_is_read_only(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path)
    config_path = tmp_path / "config.json"
    write(config_path, cfg)
    bundle = tmp_path / "review.zip"
    attempt = "12" * 32
    calls = []
    computed = dict(status="LOCAL_PROOF_COMPUTED", proving_attempt_id=attempt, published=False)
    worker = NS(c=cfg, compute_local=lambda: calls.append("compute") or computed,
                close=lambda: calls.append("close"))
    monkeypatch.setattr(zk_worker, "Worker", lambda cfg: worker)

    def check(destination, *, config):
        assert config == cfg
        calls.append(("check", Path(destination)))
        if Path(destination).exists():
            raise ValueError("ZK_PEER_BUNDLE_OUTPUT_EXISTS")
        return Path(destination)

    def export(current, destination, *, include_proof, attempt):
        assert current is worker
        calls.append(("export", Path(destination), include_proof, attempt))
        return dict(status="ZK_BUNDLE_EXPORTED", published=False)

    review_cfg = {key: cfg[key] for key in ("version", "chain_id", "evm_rpc", "evm_confirmations", "bitcoin_policy", "zk_state")}

    def verify(context, source, *, require_proof):
        assert context.c == review_cfg and not hasattr(context, "inbox")
        assert context.w3.provider.endpoint_uri == cfg["evm_rpc"]
        calls.append(("verify", Path(source), require_proof))
        return dict(status="ZK_BUNDLE_PROOF_VERIFIED", published=False)

    monkeypatch.setitem(sys.modules, "signer_agent.zk_peer_bundle", NS(check_export_destination=check,
                        export_attempt=export, verify_bundle=verify))
    flags = ["--config", str(config_path), "--bundle-file", str(bundle)]
    assert zk_worker.main(["run", "--boundary", "export", *flags]) == 0
    assert calls == [("check", bundle), "compute", ("export", bundle, True, attempt), "close"]
    assert json.loads(capsys.readouterr().out)["computation"] == computed

    calls.clear()
    bundle.touch()
    assert zk_worker.main(["run", "--boundary", "export", *flags]) == 2
    assert calls == [("check", bundle), "close"]
    assert json.loads(capsys.readouterr().out) == {"status": "BLOCKED", "reason": "ZK_PEER_BUNDLE_OUTPUT_EXISTS"}

    calls.clear()
    assert zk_worker.main(["export-bundle", *flags, "--attempt", attempt, "--without-proof"]) == 0
    assert calls == [("export", bundle, False, attempt), "close"]
    assert json.loads(capsys.readouterr().out)["status"] == "ZK_BUNDLE_EXPORTED"

    calls.clear()
    write(config_path, review_cfg)
    monkeypatch.setattr(zk_worker, "Worker", lambda cfg: pytest.fail("Bundle review must not open an inbox"))
    assert zk_worker.main(["verify-bundle", *flags, "--require-proof"]) == 0
    assert calls == [("verify", bundle, True)]
    assert json.loads(capsys.readouterr().out)["status"] == "ZK_BUNDLE_PROOF_VERIFIED"
    assert not Path(cfg["directory"]).exists()


def test_lagging_payment_observation_waits_without_blocking_other_proofs(tmp_path, monkeypatch):
    from signer_agent import zk_worker
    from signer_agent.bitcoin_rpc import BitcoinRPCError

    w = make_worker(tmp_path)
    simulated(w, monkeypatch, tmp_path)
    caught_up = False
    now = 100
    observations = []
    next_observation = w.observe_need
    waiting_request = dict(kind="deposit", consumer="x", txid="11" * 32, vout="0")
    ready_request = dict(waiting_request, txid="22" * 32)
    waiting_key = w.inbox.enqueue(waiting_request)
    ready_key = w.inbox.enqueue(ready_request)

    def observe(request, *args):
        observations.append(request["txid"])
        if request == waiting_request and not caught_up:
            raise BitcoinRPCError("BITCOIN_RPC_REJECTED", rpc_code=-5)
        return next_observation(request, *args)

    monkeypatch.setattr(zk_worker, "time", NS(time=lambda: now))
    w.observe_need = observe
    assert w.tick()["status"] == "PROOF_PREPARED"
    assert dict((k, s) for k, _, s in w.inbox.needs()) == {
        waiting_key: "WAITING_BITCOIN_RPC", ready_key: "NEEDS_STATE"}
    assert w.inbox.get("attempt")["need_ids"] == [ready_key]
    assert w.inbox.get("job_attempts").keys() == {ready_key}

    w.inbox.close()
    w.inbox = Inbox(w.c)
    block, _, adapter, _, core = w.context()
    caught_up = True
    assert w.observe_queued_need(waiting_key, waiting_request, block, adapter, core)["status"] == "WAITING_BITCOIN_RPC"
    assert observations.count(waiting_request["txid"]) == 1
    now += int(w.c["poll_seconds"])
    assert w.observe_queued_need(waiting_key, waiting_request, block, adapter, core)["status"] == "NEEDS_STATE"
    assert w.inbox.get("need-observation-retry:" + waiting_key) is None
    w.inbox.close()


def test_state_change_during_proving_never_broadcasts_old_proof(tmp_path, monkeypatch):
    w = make_worker(tmp_path); m = simulated(w, monkeypatch, tmp_path)
    w.inbox.enqueue(dict(kind="deposit", consumer="x", txid="11"*32, vout="0"))
    assert w.tick()["status"] == "PROOF_PREPARED"
    def changed(self, base):
        newer = read(base / "native.json")["after"]
        newer[7] = "ff" * 32
        m["state"] = newer
        return VERIFIER_SELECTOR + bytes(256)
    monkeypatch.setattr(LocalProver, "prove", changed)
    assert w.tick()["status"] == "REVALIDATE_STATE"
    assert m["sends"] == []
    w.inbox.close()


@pytest.mark.parametrize("phase,boundary", [
    ("PREPARED", "all"), ("PREPARED", "prepare"), ("PREPARED", "publish"),
    ("PREPARED", "prove"), ("SENDING", "all"), ("SENDING", "publish"),
])
def test_worker_keeps_attempt_on_lag_before_proving_or_publication(tmp_path, monkeypatch, phase, boundary):
    w = make_worker(tmp_path)
    model = simulated(w, monkeypatch, tmp_path)
    w.inbox.enqueue(dict(kind="deposit", consumer="unit", txid="11" * 32, vout="0"))
    assert w.advance("prepare")["status"] == "PROOF_PREPARED"
    active = dict(w.inbox.get("attempt"), phase=phase)
    w.inbox.put("attempt", active)
    w.inbox.reserve("existing-reservation", 100)
    observed = w.context()

    def advance():
        return w.tick() if boundary == "all" else w.advance(boundary)

    w.context = lambda **kw: (NS(number=0, hash=bytes(32)), *observed[1:])
    assert advance()["status"] == "AWAITING_EVM_RPC"
    assert w.inbox.get("attempt") == active
    if phase == "PREPARED":
        w.context = lambda **kw: (*observed[:-1], {"blocks": active["target_height"] - 1})
        assert advance()["status"] == "WAITING_BITCOIN_RPC"
        assert w.inbox.get("attempt") == active
    assert model["proves"] == 0 and not model["sends"]
    assert dict(w.inbox.db.execute("SELECT * FROM reservations")) == {"existing-reservation": "100"}
    w.inbox.close()


def test_worker_waits_on_lag_after_proving_and_keeps_attempt(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    model = simulated(w, monkeypatch, tmp_path)
    w.inbox.enqueue(dict(kind="deposit", consumer="unit", txid="11" * 32, vout="0"))
    assert w.tick()["status"] == "PROOF_PREPARED"
    active = w.inbox.get("attempt")
    observed = w.context()

    def proof_with_lag(self, base):
        w.context = lambda **kw: (*observed[:-1], {"blocks": active["target_height"] - 1})
        return VERIFIER_SELECTOR + bytes(256)

    monkeypatch.setattr(LocalProver, "prove", proof_with_lag)
    assert w.tick()["status"] == "WAITING_BITCOIN_RPC"
    assert w.inbox.get("attempt") == active
    assert not model["sends"]
    w.inbox.close()


def test_worker_recovers_persisted_publication_before_reproving(tmp_path, monkeypatch):
    from signer_agent.operations import Journal
    from signer_agent.zk_spv_cli import transition_step

    w = make_worker(tmp_path)
    model = simulated(w, monkeypatch, tmp_path)
    w.inbox.enqueue(dict(kind="deposit", consumer="unit", txid="11" * 32, vout="0"))
    assert w.tick()["status"] == "PROOF_PREPARED"
    active = dict(w.inbox.get("attempt"), phase="SENDING")
    w.inbox.put("attempt", active)
    base = w.inbox.path / "attempts" / active["id"]
    name = "zk-worker-" + active["id"]
    step = transition_step(w.c["chain_id"], w.c["zk_state"], w.c["bitcoin_policy"],
                           (base / "state.journal").read_bytes(), VERIFIER_SELECTOR + bytes(256))
    Journal(w.inbox.path / "transactions.sqlite").put(name + ":transition", step)

    def reconcile(target, saved, records, *, recover_only):
        assert recover_only and target == name and saved == step
        return dict(status="BROADCAST")

    w.publish = reconcile
    assert w.tick() == dict(status="BROADCAST", proving_attempt_id=active["id"])
    assert w.inbox.get("attempt") == active and model["proves"] == 0
    w.inbox.close()


@pytest.mark.parametrize(("core_state", "delivered", "expected"), [
    ("same", False, "BROADCAST"), ("fork", False, "REVALIDATE_BITCOIN"),
    ("behind", False, "WAITING_BITCOIN_RPC"), ("inconsistent", False, "WAITING_BITCOIN_RPC"),
    ("offline", False, "WAITING_BITCOIN_RPC"), ("offline", True, "VERIFIED"),
])
def test_transition_publication_rechecks_frozen_target_only_to_send(tmp_path, monkeypatch, core_state, delivered, expected):
    from signer_agent.bitcoin_rpc import BitcoinRPCError
    from signer_agent.bootstrap import Bootstrap
    from signer_agent.bootstrap_parallel import require_publication_context

    c = config(tmp_path)
    worker = Worker.__new__(Worker)
    worker.c = c
    worker.inbox = Inbox(c)
    active = dict(id="01" * 32, phase="SENDING", target_height=32257, target_hash="aa" * 32)
    worker.inbox.put("attempt", active)
    prefix = "zk-worker-" + active["id"]
    step = dict(id="transition", kind="call", contract="relay", method="submitTransition", args=["0x01", "0x02"])
    observations, sends = [], []

    def rpc(method, *args):
        observations.append((method, args))
        if core_state == "offline":
            raise BitcoinRPCError("BITCOIN_RPC_FAILED")
        if method == "getblockchaininfo":
            height = 32256 if core_state == "behind" else 32258
            return dict(chain="main", blocks=height, headers=height, initialblockdownload=False, bestblockhash="11" * 32)
        assert method == "getblockhash"
        if args == (32257,):
            return ("bb" if core_state == "fork" else "aa") * 32
        assert args == (32258,)
        return ("12" if core_state == "inconsistent" else "11") * 32

    worker.bitcoin = NS(call=rpc)

    def init(runner, profile, path):
        assert profile["deployment_id"] == prefix
        runner.journal = NS(path=path)

    def apply(runner, identity, *, before_submit):
        assert before_submit([step]) is True
        if delivered:
            return dict(status="VERIFIED")
        require_publication_context(runner, runner.journal, prefix, step)
        sends.append(step)
        return dict(status="BROADCAST")

    monkeypatch.setattr(Bootstrap, "__init__", init)
    monkeypatch.setattr(Bootstrap, "apply", apply)
    try:
        assert worker.publish(prefix, step, {}, recover_only=True)["status"] == expected
        assert sends == ([step] if expected == "BROADCAST" else [])
        assert worker.inbox.get("attempt") == active
        assert bool(observations) is not delivered
    finally:
        worker.inbox.close()


def test_shared_identity_lane_observes_delivered_transition_but_needs_worker_to_rebroadcast(tmp_path):
    from eth_account import Account
    from hexbytes import HexBytes
    from web3.exceptions import TransactionNotFound

    from signer_agent.bootstrap_parallel import recover_identity, save_signed
    from signer_agent.operations import Journal

    account = Account.create()
    prefix = "zk-worker-" + "01" * 32
    step = dict(id="transition", kind="call", contract="relay", method="submitTransition", args=["0x01", "0x02"])
    path = Journal(tmp_path / "worker.sqlite")
    key = prefix + ":transition"
    path.put(key, step)
    lane = Journal(tmp_path / "identity.sqlite")
    tx = account.sign_transaction(dict(chainId=31337, nonce=0, gasPrice=1, gas=21000,
                                       to=account.address, value=0, data="0x01"))
    result = dict(tx_hash=tx.hash.hex(), raw_transaction=tx.raw_transaction.hex(), nonce="0")
    sends, receipts = [], {}

    def receipt(digest):
        if digest not in receipts:
            raise TransactionNotFound(digest)
        return receipts[digest]

    def unknown(digest):
        raise TransactionNotFound(digest)

    block_hash = HexBytes("0x" + "a5" * 32)
    w3 = NS(eth=NS(chain_id=31337, get_transaction_receipt=receipt, get_transaction=unknown,
                   get_block=lambda number: NS(number=number, hash=block_hash)))
    bootstrap = NS(w3=w3, journal=path, broadcast=sends.append)
    save_signed(lane, bootstrap, key, step, result)

    with pytest.raises(ValueError, match="ZK_TRANSACTION_RECOVERY_REQUIRED"):
        recover_identity(lane, bootstrap, account)
    assert not sends
    assert path.get(key)["result"] == result

    receipts[result["tx_hash"]] = NS(transactionHash=tx.hash, blockHash=block_hash, blockNumber=1, status=1)
    recover_identity(lane, bootstrap, account)
    assert not sends
    assert path.get(key)["result"] == result


@pytest.mark.parametrize("work", ["other_consumer", "heartbeat", "none"])
def test_exhausted_proof_budget_preserves_other_work_and_prior_obligation(tmp_path, monkeypatch, work):
    from signer_agent import zk_worker_heartbeat

    w = make_worker(tmp_path)
    model = simulated(w, monkeypatch, tmp_path)
    exhausted = w.inbox.enqueue(dict(kind="deposit", consumer="unit", txid="11" * 32, vout="0"))
    w.c["max_attempts"] = "1"
    old_history = ["ab" * 32]
    w.inbox.put("job_attempts", {exhausted: old_history})
    old_obligation = {"deadline": 100, "created_at": 0, "origins": {"local": 100}}
    w.inbox.put("obligation:" + exhausted, old_obligation)
    if work == "other_consumer":
        eligible = w.inbox.enqueue(dict(kind="deposit", consumer="unit", txid="22" * 32, vout="0"))
    elif work == "heartbeat":
        eligible = "heartbeat:reviewed"
        w.c["heartbeat"] = {"enabled": True}
        monkeypatch.setattr(zk_worker_heartbeat, "observe", lambda *a:
                            dict(service_job_id=eligible, deadline=100, broadcast_not_before=10))
    prepared = w.advance("prepare")
    assert w.inbox.needs()[0][2] == "ATTEMPT_BUDGET_REVIEW_REQUIRED"
    history = w.inbox.get("job_attempts")
    assert history[exhausted] == old_history and w.inbox.get("obligation:" + exhausted) == old_obligation
    if work == "none":
        assert prepared["status"] == "ATTEMPT_BUDGET_REVIEW_REQUIRED"
        assert w.inbox.get("attempt") is None
    else:
        assert prepared["status"] == "PROOF_PREPARED"
        assert w.inbox.get("attempt")["need_ids"] == [eligible]
        assert history[eligible] == [prepared["proving_attempt_id"]]
    assert model["proves"] == 0 and not model["sends"]
    w.inbox.close()


def test_worker_lock_excludes_parallel_processes(tmp_path):
    a = make_worker(tmp_path); b = make_worker(tmp_path)
    with a.exclusive():
        with pytest.raises(ValueError, match="WORKER_BUSY"):
            with b.exclusive(): pass
    a.inbox.close(); b.inbox.close()


@pytest.mark.parametrize("issuer_kind", ["MiningPoolV1", "ConfederationBitcoinReserve"])
def test_consumer_admission_checks_script_actual_tx_inclusion_and_depth(tmp_path, monkeypatch, issuer_kind):
    """Actual recorded regtest tx bytes exercise parsing; RPC/graph are unit fakes."""
    from signer_agent.bitcoin_network import header_hash
    from signer_agent import crypto
    root = ROOT / "rust/zkspv/deposit-integration/evidence/run-04"
    row = read(root / "funding.json")[0]
    decoded = crypto.call("bitcoin_funding", raw=row["raw_tx"], txid=row["txid"])
    raw = (root / "block-2017.bin").read_bytes()[:80]
    w = Worker.__new__(Worker); w.c = config(tmp_path)
    pool = next(iter(w.c["allowed_consumers"])); adapter = NS(address="adapter")
    w.c["allowed_consumers"][pool] = issuer_kind
    values = dict(deposits="deposits", escrow="escrow", spv="adapter", depth=7, consumed=False,
                  payoutScript=bytes.fromhex(decoded["outputs"][row["vout"]]["script"]))
    class Functions:
        def __getattr__(self, name):
            return lambda *args: NS(call=lambda **kw: values[name])
    w.contract = lambda *args: NS(functions=Functions())
    adapter.functions = NS(getBlockStatus=lambda *args: NS(call=lambda **kw: [True, 8]))
    display = header_hash(raw)[::-1].hex()
    core = dict(blocks=32264, bestblockhash="aa"*32)
    tx = dict(hex=row["raw_tx"], blockhash=display)
    def rpc(method, *args):
        if method == "getrawtransaction": return tx
        if method == "getblockheader": return dict(height=32257) if args[1] else raw.hex()
        if method == "getblockhash": return display if args[0] == 32257 else core["bestblockhash"]
        raise AssertionError(method)
    w.bitcoin = NS(call=rpc)
    proof = dict(block_hash=header_hash(raw).hex(), siblings=row["siblings"], index=str(row["index"]))
    monkeypatch.setattr("signer_agent.bitcoin_proofs.inclusion", lambda *a: proof)
    request = dict(kind="deposit", consumer=pool, txid=row["txid"], vout=str(row["vout"]))
    assert w.observe_need(request, NS(number=1), adapter, core)["status"] == "READY"
    values["payoutScript"] = bytes(34)
    with pytest.raises(ValueError, match="DEPOSIT_BINDING"): w.observe_need(request, NS(number=1), adapter, core)
    values["payoutScript"] = bytes.fromhex(decoded["outputs"][row["vout"]]["script"])
    values["spv"] = "different"
    with pytest.raises(ValueError, match="STATE_MISMATCH"): w.observe_need(request, NS(number=1), adapter, core)
    values["spv"] = "adapter"; core["blocks"] = 32257
    assert w.observe_need(request, NS(number=1), adapter, core)["status"] == "WAITING_BITCOIN"
    core["blocks"] = 32264; proof["siblings"] = ["00"*32]
    with pytest.raises(ValueError, match="NEED_INCLUSION"): w.observe_need(request, NS(number=1), adapter, core)
    with pytest.raises(ValueError, match="CONSUMER_NOT_ALLOWED"):
        w.observe_need(dict(request, consumer="unapproved"), NS(number=1), adapter, core)


def test_terminal_halt_persisted_without_prover_or_money(tmp_path, monkeypatch):
    w = make_worker(tmp_path); m = simulated(w, monkeypatch, tmp_path)
    m["state"][8] = True
    assert w.tick() == dict(status="HALTED", confirmed=True)
    assert w.inbox.get("terminal_halt")["state"][8]
    assert m["sends"] == [] and m["proves"] == 0
    w.inbox.close()


def prover_fixture(tmp_path, monkeypatch):
    """Stage-control unit tests only; synthetic receipts are never EVM evidence."""
    from signer_agent import zk_worker_prover as module
    c = config(tmp_path); Path(c["directory"]).mkdir(mode=0o700)
    write(Path(c['identity_file']),dict(private_key='01'.zfill(64)))
    base = tmp_path / "attempt"; base.mkdir()
    (base / "state.input").write_bytes(b"unit input")
    (base / "state.journal").write_bytes(b"unit journal")
    write(base / "preparation.json", dict(source_sha256={}, input_sha256=sha(base / "state.input"),
        journal_sha256=sha(base / "state.journal"), program_image_id="aa"*32))
    p = LocalProver(c)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(p, "preflight", lambda b: None)
    monkeypatch.setattr(module.shutil, "disk_usage", lambda b: NS(free=100*1024**3))
    return module, p, base


def stage_report(base, stage, status="VERIFIED"):
    folder = base / stage; folder.mkdir(exist_ok=True)
    (folder / "receipt.bin").write_bytes(stage.encode())
    write(folder / "report.json", dict(status=status, preparation_sha256=sha(base / "preparation.json"),
        artifacts={"receipt.bin": dict(sha256=sha(folder / "receipt.bin"))}))


def test_prover_stages_resume_then_verify_cache_each_time(tmp_path, monkeypatch):
    module, p, base = prover_fixture(tmp_path, monkeypatch)
    stage_report(base, "composite")
    origin=p.origins();folder=base/'composite'
    write(folder/'local-origin.json',origin.sign(origin.payload(base,folder/'receipt.bin',stage='composite',report=folder/'report.json')))
    write(base / "composite.running.json", {})
    runs, checks = [], []
    def run(command, **kwargs):
        stage = command[3]; runs.append(stage); stage_report(base, stage)
        folder=base/stage
        write(folder/'local-origin.json',origin.sign(origin.payload(base,folder/'receipt.bin',stage=stage,report=folder/'report.json')))
        assert kwargs["cwd"] == ROOT and command[-1] == p.config["stage_timeout_seconds"]
        return NS(returncode=0)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(p, "verify", lambda b, r, o: checks.append(sha(r)) or bytes(260))
    assert p.prove(base) == bytes(260)
    assert runs == ["succinct", "groth16"] and not read(base / "proof-result.json")["cache_hit"]
    assert not (base / "composite.running.json").exists()
    assert p.prove(base) == bytes(260) and len(checks) == 2
    assert runs == ["succinct", "groth16"] and read(base / "proof-result.json")["cache_hit"]
    def reject(*a): raise ValueError("ZK_RECEIPT_NOT_VERIFIED")
    monkeypatch.setattr(p, "verify", reject)
    with pytest.raises(ValueError, match="RECEIPT_NOT_VERIFIED"): p.prove(base)
    assert runs == ["succinct", "groth16"]  # no silent fallback on a corrupt cache


def test_prover_ambiguous_stage_requires_recovery(tmp_path, monkeypatch):
    module, p, base = prover_fixture(tmp_path, monkeypatch)
    write(base / "composite.running.json", {})
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("must not duplicate a stage"))
    with pytest.raises(ValueError, match="STAGE_RECOVERY_REQUIRED"): p.prove(base)


def test_prover_failed_stage_retries_are_bounded(tmp_path, monkeypatch):
    module, p, base = prover_fixture(tmp_path, monkeypatch)
    p.config["max_stage_retries"] = "1"
    stage_report(base, "composite", "INCOMPLETE")
    runs = []
    def fail(command, **kwargs):
        runs.append(command[3]); stage_report(base, command[3], "INCOMPLETE")
        return NS(returncode=1)
    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(ValueError, match="PROVER_STAGE_FAILED"): p.prove(base)
    with pytest.raises(ValueError, match="STAGE_RETRY_BUDGET"): p.prove(base)
    assert runs == ["composite"]


def test_prover_changed_stage_artifact_is_rejected(tmp_path, monkeypatch):
    module, p, base = prover_fixture(tmp_path, monkeypatch)
    stage_report(base, "composite")
    (base / "composite/receipt.bin").write_bytes(b"tampered")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="STAGE_ARTIFACT_DRIFT"): p.prove(base)


def test_prover_zero_exit_without_completion_evidence_is_not_success(tmp_path, monkeypatch):
    module, p, base = prover_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: NS(returncode=0))
    with pytest.raises(ValueError, match="STAGE_RECOVERY_REQUIRED"): p.prove(base)
    assert (base / "composite.running.json").exists()

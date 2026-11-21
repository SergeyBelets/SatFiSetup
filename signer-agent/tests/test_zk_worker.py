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
        m["state"] = list(m["state"]); m["state"][7] = "ff"*32
        return VERIFIER_SELECTOR + bytes(256)
    monkeypatch.setattr(LocalProver, "prove", changed)
    assert w.tick()["status"] == "REVALIDATE_STATE"
    assert m["sends"] == []
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

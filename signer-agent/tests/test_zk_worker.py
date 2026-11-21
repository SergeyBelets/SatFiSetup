"""Local inbox, budgets, native witness construction and worker recovery."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace as NS

import pytest
from web3 import Web3
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


def test_consumer_admission_checks_script_actual_tx_inclusion_and_depth(tmp_path, monkeypatch):
    """Actual recorded regtest tx bytes exercise parsing; RPC/graph are unit fakes."""
    from signer_agent.bitcoin_network import header_hash
    from signer_agent import crypto
    root = ROOT / "rust/zkspv/deposit-integration/evidence/run-04"
    row = read(root / "funding.json")[0]
    decoded = crypto.call("bitcoin_funding", raw=row["raw_tx"], txid=row["txid"])
    raw = (root / "block-2017.bin").read_bytes()[:80]
    w = Worker.__new__(Worker); w.c = config(tmp_path)
    pool = next(iter(w.c["allowed_consumers"])); adapter = NS(address="adapter")
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

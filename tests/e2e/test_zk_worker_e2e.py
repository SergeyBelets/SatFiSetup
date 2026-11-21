"""Developer-only historical receipt verification and foreign-proof refusal.

The recorded receipt is not shipped to participants and cannot be adopted as
this worker's own computation. Native preparation and crypto verification are
real; the inbox observation and Bitcoin wall clock are historical fixtures.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest

from web3 import Web3
from signer_agent.bootstrap import artifact
from signer_agent.zk_spv import constructor_args, MAINNET_IMAGE
from signer_agent.zk_worker import Worker, state_wire
from signer_agent.zk_worker_prover import ROOT, LocalProver, sha
from test_mainnet_bootstrap import HistoricalFixtureCore
sys.path.insert(0, str(ROOT / "signer-agent/tests"))
from test_zk_worker import config


def test_historical_receipt_cannot_be_published_as_own(tmp_path, monkeypatch):
    c = config(tmp_path)
    c["bitcoin_policy"]["max_headers_per_sync"] = "1"
    core = HistoricalFixtureCore(); core.height = 32356
    core.by_hash = {core.record(h)["hash"]: h for h in range(32246, core.height+1)}
    core.policy = c["bitcoin_policy"]
    # Host and EVM use the same explicit historical wall clock. The partial
    # 32257 update stays non-Fresh until later chunks catch up to this tip.
    timestamp = core.record(core.height)["time"]
    monkeypatch.setattr("signer_agent.bitcoin_network.time.time", lambda: timestamp)
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    c["evm_rpc"] = f"http://127.0.0.1:{port}"
    source_paths = sorted((ROOT / "signer-agent/signer_agent").glob("*.py"))
    source_paths += sorted((ROOT / "signer-agent/tests").glob("*.py"))
    source_paths += [Path(__file__), ROOT / "rust/zkspv/worker-host/src/main.rs",
        ROOT / "rust/zkspv/worker-host/Cargo.lock", ROOT / "rust/zkspv/worker-host/Cargo.toml",
        ROOT / "ops/zkspv/worker_build.py", ROOT / "ops/zkspv/state_trial_prove.py"]
    sources = {str(p.relative_to(ROOT)): sha(p) for p in source_paths}
    log = (tmp_path / "anvil.log").open("w")
    anvil = os.environ.get("SATOHSI_ANVIL") or shutil.which("anvil")
    if not anvil:
        raise RuntimeError("Anvil required: set SATOHSI_ANVIL or PATH")
    node = subprocess.Popen([anvil, "--host", "127.0.0.1", "--port", str(port),
        "--chain-id", "31337", "--timestamp", str(timestamp), "--hardfork", "osaka", "--silent"], stdout=log, stderr=subprocess.STDOUT)
    worker = None
    try:
        w3 = Web3(Web3.HTTPProvider(c["evm_rpc"]))
        for _ in range(100):
            if w3.is_connected(): break
            time.sleep(.05)
        assert w3.is_connected()
        identity = Path(c["identity_file"])
        identity.write_text(json.dumps(dict(private_key="ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")))
        identity.chmod(0o600)
        cookie = Path(c["bitcoin_cookie"]); cookie.write_text("fixture:unused"); cookie.chmod(0o600)
        a = artifact("ZkBitcoinStateV2")
        r = w3.eth.wait_for_transaction_receipt(w3.eth.contract(abi=a["abi"], bytecode=a["bytecode"]["object"])
            .constructor(*constructor_args(c["bitcoin_policy"])).transact({"from": w3.eth.accounts[0]}))
        assert r.status == 1 and r.contractAddress == c["zk_state"]
        worker = Worker(c); worker.bitcoin = core
        request = dict(kind="deposit", consumer=next(iter(c["allowed_consumers"])), txid="11"*32, vout="0")
        key = worker.inbox.enqueue(request)
        worker.observe_need = lambda *args: dict(status="NEEDS_STATE", height=32257, depth=6,
                                                header=core.raw(32257).hex())
        result = worker.tick(); assert result["status"] == "PROOF_PREPARED"
        attempt = worker.inbox.get("attempt"); base = worker.inbox.path / "attempts" / attempt["id"]
        reference = ROOT / "rust/zkspv/state-proof-integration/evidence/local-state"
        assert (base / "state.input").read_bytes() == (reference / "state.input").read_bytes()
        assert (base / "state.journal").read_bytes() == (reference / "state.journal").read_bytes()
        cache = worker.inbox.path / "receipt-cache"; cache.mkdir()
        cache_key = hashlib.sha256(bytes.fromhex(MAINNET_IMAGE) + (base / "state.journal").read_bytes()).hexdigest()
        shutil.copyfile(reference / "groth16/receipt.bin", cache / (cache_key + ".bin"))
        # An imported legacy cache is valid public evidence, not our proof.
        worker.inbox.close(); worker = Worker(c); worker.bitcoin = core
        assert worker.inbox.get("attempt") == attempt
        prover=LocalProver(c)
        from signer_agent.zk_spv_cli import transition_step
        seal=(reference / "groth16/verified/seal.bin").read_bytes()
        with pytest.raises(ValueError,match="ZK_VERIFIER_3_1_REQUIRED"):
            transition_step(31337,c["zk_state"],c["bitcoin_policy"],(base/"state.journal").read_bytes(),seal)
        state=w3.eth.contract(address=c["zk_state"],abi=a["abi"])
        nonce=w3.eth.get_transaction_count(w3.eth.accounts[0])
        with pytest.raises(ValueError,match="ZK_LOCAL_PROOF_ORIGIN_REQUIRED"):
            prover.verify_own(base,base / "publication-verified")
        assert state.functions.stateVersion().call()==0
        assert w3.eth.get_transaction_count(w3.eth.accounts[0])==nonce
        assert worker.inbox.get("attempt")==attempt
        assert sources=={str(p.relative_to(ROOT)):sha(p) for p in source_paths}
        destination = os.environ.get("ZK_WORKER_REPORT")
        if destination:
            result = dict(status="HISTORICAL_RECEIPT_FOREIGN_ORIGIN_REFUSED", production_ready=False, live_core=False,
                consumer_observation="SIMULATED_PREVALIDATED_INBOX", fresh_proving_performed=False,
                proof_source="DEVELOPER_ONLY_HISTORICAL_RECEIPT_NOT_PARTICIPANT_COMPUTATION",
                actual_native_input_matches_qualified_input=True, historical_clock=timestamp,
                state=c["zk_state"], state_version=0, service_job_id=key,
                proving_attempt_id=attempt["id"], public_transactions=0,
                checks=["native_archive_replay", "restart_after_prepare", "legacy_selector_refused",
                        "foreign_receipt_not_accepted_as_own", "no_state_publication"], source_sha256=sources)
            Path(destination).write_text(json.dumps(result, indent=2)+"\n")
    finally:
        if worker: worker.inbox.close()
        node.terminate()
        try: node.wait(timeout=10)
        except subprocess.TimeoutExpired: node.kill(); node.wait()
        log.close()

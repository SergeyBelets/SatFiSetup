"""Local production-code slice: real mainnet Groth16 + ordinary DKG consumers.

Historical Bitcoin data and an explicit historical EVM clock. No live Core,
public deployment, fake receipt, test state, or monetary mainnet mint claim.
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
from types import SimpleNamespace

import pytest
from web3 import Web3

from signer_agent.bootstrap import Bootstrap, artifact
from signer_agent.bootstrap_plan import build
from signer_agent.bitcoin_trust import inspect_registry, graph_options
from signer_agent.contract_provenance import check_provenance_anchors
from signer_agent.zk_spv import inspect_binding, review_consumer
from signer_agent.zk_spv_cli import transition_step, materialization_step
from ops.zkspv.state_trial import membership, journal_state

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "signer-agent/tests"))
from test_zk_bootstrap import zk_invite, profile


def test_real_mainnet_proof_bootstrap_and_consumer_graph(tmp_path, monkeypatch):
    sources = (list((ROOT / "signer-agent/signer_agent").glob("*.py"))
        + list((ROOT / "web3/contracts/src").rglob("*.sol"))
        + list((ROOT / "rust/zkspv/state-adapter/contracts").glob("*.sol"))
        + [Path(__file__), ROOT / "signer-agent/tests/test_zk_bootstrap.py",
           ROOT / "ops/zkspv/state_trial.py", ROOT / "ops/zkspv/deposit_trial.py",
           ROOT / "tests/e2e/test_dkg_processes.py", ROOT / "tests/e2e/test_bitcoin_processes.py",
           ROOT / "web3/foundry.toml", ROOT / "rust/zkspv/state-adapter/foundry.toml"])
    source_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    anvil = os.environ.get("SATOHSI_ANVIL") or shutil.which("anvil")
    if not anvil:
        raise RuntimeError("Anvil required: set SATOHSI_ANVIL or PATH")
    supplied = os.environ.get("SATOHSI_ZK_ACCEPTANCE_RECEIPT_DIR")
    evidence = Path(supplied) if supplied else ROOT / "rust/zkspv/state-proof-integration/evidence/local-state"
    journal = (evidence / "state.journal").read_bytes()
    seal = (evidence / "groth16/verified/seal.bin").read_bytes()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    rpc = f"http://127.0.0.1:{port}"
    log = (tmp_path / "anvil.log").open("w")
    node = subprocess.Popen([anvil, "--host", "127.0.0.1", "--port", str(port), "--chain-id", "31337",
        "--timestamp", str(journal_state(journal, True)[5] - 1000), "--hardfork", "osaka",
        "--gas-limit", "16777216", "--silent"], stdout=log, stderr=subprocess.STDOUT)
    try:
        w3 = Web3(Web3.HTTPProvider(rpc))
        for _ in range(100):
            if w3.is_connected(): break
            time.sleep(.05)
        assert w3.is_connected()
        identity = tmp_path / "operator.json"
        # Public Anvil fixture key, never a funded network identity.
        identity.write_text(json.dumps(dict(private_key="ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")))
        identity.chmod(0o600)
        invite = zk_invite(); policy = invite["bitcoin_policy"]
        plan = build(profile(invite)); steps = {s["id"]: s for s in plan["steps"]}
        base = dict(mode="backed", bitcoin_network="mainnet", bitcoin_policy=policy,
                    chain_id="31337", evm_rpc=rpc, evm_confirmations="1")
        state_profile = dict(base, deployment_id="zk-mainnet-bootstrap-state", steps=[steps["relay"]])
        runner = Bootstrap(state_profile, tmp_path / "state.sqlite")
        assert runner.apply(identity)["status"] == "VERIFIED"
        state = runner.contract("relay"); adapter = runner.contract("spv_guard")
        assert state.address == Web3.to_checksum_address("0x" + journal[108:128].hex())
        _, _, records, proofs = inspect_binding(w3, state.address, policy, w3.eth.block_number)
        check_provenance_anchors(w3, dict(contracts=records), proofs)
        assert not state.functions.isFresh().call()
        nonce = w3.eth.get_transaction_count(w3.eth.accounts[0])
        assert runner.apply(identity)["status"] == "VERIFIED"
        assert w3.eth.get_transaction_count(w3.eth.accounts[0]) == nonce
        altered_policy = json.loads(json.dumps(policy))
        altered_policy["zk_spv"]["max_state_age_seconds"] = "3601"
        with pytest.raises(ValueError, match="ZK_STATE_POLICY_MISMATCH"):
            inspect_binding(w3, state.address, altered_policy, w3.eth.block_number)
        with pytest.raises(ValueError, match="ZK_ADAPTER_BINDING_MISMATCH"):
            inspect_binding(w3, state.address, policy, w3.eth.block_number, adapter_address=w3.eth.accounts[1])
        def apply_step(name, step):
            r = Bootstrap(dict(base, deployment_id=name, contracts=records, steps=[step]), tmp_path / (name + ".sqlite"))
            assert r.apply(identity)["status"] == "VERIFIED"
            return r
        proof_runner = None
        if supplied:
            step = transition_step(31337, state.address, policy, journal, seal)
            proof_runner = apply_step("mainnet-transition", step)
            assert tuple(state.functions.getState().call()) == journal_state(journal, True)
            nonce = w3.eth.get_transaction_count(w3.eth.accounts[0])
            assert proof_runner.apply(identity)["status"] == "VERIFIED"
            assert w3.eth.get_transaction_count(w3.eth.accounts[0]) == nonce
            assert state.functions.isFresh().call()
            raw, path = membership(journal)
            header = dict(height="32257", header=raw.hex(), siblings=[s.hex() for s in path])
            apply_step("mainnet-header", materialization_step(header))
            assert adapter.functions.getBlockStatus(journal_state(journal, True)[2]).call() == [True, 1]
        else:
            from web3.exceptions import ContractLogicError, Web3RPCError
            with pytest.raises(ValueError, match="ZK_VERIFIER_3_1_REQUIRED"):
                transition_step(31337, state.address, policy, journal, seal)
            with pytest.raises((ContractLogicError, Web3RPCError)):
                state.functions.submitTransition(journal, seal).call()
            assert state.functions.stateVersion().call() == 0

        from ops.zkspv.deposit_trial import helpers
        dkg, authorize, _, _ = helpers(rpc)
        def deploy(name, *args):
            a = artifact(name)
            receipt = w3.eth.wait_for_transaction_receipt(w3.eth.contract(abi=a["abi"], bytecode=a["bytecode"]["object"])
                .constructor(*args).transact({"from": w3.eth.accounts[0]}))
            assert receipt.status == 1
            return w3.eth.contract(address=receipt.contractAddress, abi=a["abi"])
        def tx(call):
            receipt = w3.eth.wait_for_transaction_receipt(call.transact({"from": w3.eth.accounts[0]}))
            assert receipt.status == 1
        registry = deploy("DKGCoordinator"); frost = deploy("FROSTVerifier")
        coordinator = deploy("FROSTCoordinator", frost.address)
        tx(coordinator.functions.bindDKGRegistry(registry.address))
        evm_dir = tmp_path / "evm"; evm_dir.mkdir()
        btc_dir = tmp_path / "btc"; btc_dir.mkdir()
        evm, participant, evm_public = dkg.ceremony(evm_dir, w3, registry, 4, 3, "EVM_AUTH")
        btc, _, btc_public = dkg.ceremony(btc_dir, w3, registry, 4, 3, "BTC_SPEND")
        auth = deploy("DKGAuthorization", coordinator.address, evm.session_id,
                      bytes.fromhex(evm_public["group"])[1:], list(evm.participants), 3)
        bindings = dict(records, conf_auth=dict(address=auth.address, artifact="DKGAuthorization"),
                        dkg=dict(address=registry.address, artifact="DKGCoordinator"))
        consumer_profile = profile(invite)
        consumer_profile["btc_ceremony"]["id"] = "0x" + btc.session_id.hex()
        consumer_plan = {s["id"]: s for s in build(consumer_profile)["steps"]}
        consumer_steps = [consumer_plan[n] for n in ("batches", "escrow", "deposits", "mining")]
        consumer = Bootstrap(dict(base, deployment_id="zk-mainnet-consumer", contracts=bindings,
                             steps=consumer_steps), tmp_path / "consumer.sqlite")
        assert consumer.apply(identity)["status"] == "VERIFIED"
        batches = consumer.contract("batches"); escrow = consumer.contract("escrow")
        deposits = consumer.contract("deposits"); mining = consumer.contract("mining")
        def approve(c, kind, helper, args):
            return authorize(w3, coordinator, auth, participant, tmp_path, c.address, kind, helper, args)
        op = approve(batches, "BitcoinBatchRegistry", "getSetWithdrawalEscrowOperationHash", [escrow.address])
        tx(batches.functions.setWithdrawalEscrow(escrow.address, op))
        op = approve(escrow, "BitcoinWithdrawalEscrow", "getSetDepositRegistryOperationHash", [deposits.address])
        tx(escrow.functions.setDepositRegistry(deposits.address, op))
        completed = registry.functions.getCompletedResult(btc.session_id).call()
        key = dict(result_hash=bytes(completed[1]).hex(), result=btc_public)
        inspected = inspect_registry(btc, key, SimpleNamespace(w3=w3), batches.address,
            bitcoin_network="mainnet", bitcoin_policy=policy, evm_confirmations="1")
        assert inspected["graph"]["contracts"]["spv_guard"]["artifact"] == "ZkSpvAdapter"
        assert graph_options(inspected["graph"])["bitcoin_policy"] == policy
        check_provenance_anchors(w3, inspected["graph"], inspected["provenance"])
        assert deposits.functions.spv().call() == escrow.functions.spv().call() == adapter.address
        assert mining.functions.deposits().call() == deposits.address
        review_consumer(w3, invite, mining, "MiningPoolV1", {}, w3.eth.block_number)
        legacy = dict(policy, version="1"); legacy.pop("zk_spv")
        with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
            review_consumer(w3, dict(invite, bitcoin_policy=legacy), mining, "MiningPoolV1", {}, w3.eth.block_number)
        with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
            inspect_registry(btc, key, SimpleNamespace(w3=w3), batches.address,
                             bitcoin_network="mainnet", bitcoin_policy=legacy, evm_confirmations="1")
        w3.provider.make_request("evm_increaseTime", [3601]); w3.provider.make_request("evm_mine", [])
        assert not adapter.functions.isFresh().call()
        assert adapter.functions.getBlockStatus(journal_state(journal, True)[2]).call() == [False, 0]
        destination = os.environ.get("ZK_BOOTSTRAP_REPORT")
        assert source_hashes == {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
        if destination:
            report = dict(status="LOCAL_MAINNET_ZK_BOOTSTRAP_CONSUMER_PASS" if supplied else "LOCAL_LEGACY_ZK_PROOF_REFUSED", production_ready=False,
                public_transactions=0, historical_evm_clock=True, live_core_tested=False, mainnet_mint_tested=False,
                policy=policy, graph=inspected["graph"], provenance=inspected["provenance"],
                checks=["bootstrap_state_deploy", "exact_constructor_and_child_provenance", "wrong_policy_rejected",
                        "wrong_adapter_rejected", "real_mainnet_groth16" if supplied else "legacy_groth16_refused",
                        "durable_restart_no_duplicate", "real_3_of_4_dkg_and_frost_bindings", "bootstrap_consumer_deploy",
                        "participant_graph_pinned", "consumer_policy_review", "legacy_profile_mismatch_rejected", "fresh_expired"],
                journal_sha256=hashlib.sha256(journal).hexdigest(), seal_sha256=hashlib.sha256(seal).hexdigest(),
                state_deployment=runner.journal.get("zk-mainnet-bootstrap-state:relay")["result"],
                proof_acceptance=proof_runner.journal.get("mainnet-transition:transition")["result"] if proof_runner else None,
                source_sha256=source_hashes)
            Path(destination).write_text(json.dumps(report, indent=2) + "\n")
    finally:
        node.terminate()
        try: node.wait(timeout=10)
        except subprocess.TimeoutExpired: node.kill(); node.wait()
        log.close()

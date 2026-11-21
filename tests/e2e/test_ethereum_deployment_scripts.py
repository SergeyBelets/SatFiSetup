"""Real Forge broadcasts through the production Ethereum transaction budget gate.

The chain-id-1 node and artificial account are isolated and have no public peers.
Anvil's finalized tag exercises protocol handling, not Ethereum consensus.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

from web3 import Web3

from script_helpers import SENDER, script_project

ROOT = Path(__file__).resolve().parents[2]


def test_ethereum_staged_forge_budget_and_finalized_retry(tmp_path, record_property):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    rpc = f"http://127.0.0.1:{port}"
    w3 = Web3(Web3.HTTPProvider(rpc))
    log_path = tmp_path / "ethereum-forge-node.log"
    with log_path.open("w") as node_log:
        process = subprocess.Popen([shutil.which("anvil"), "--host", "127.0.0.1", "--port", str(port),
            "--chain-id", "1", "--hardfork", "osaka", "--gas-limit", "30000000",
            "--max-persisted-states", "100000", "--cache-path", str(tmp_path / "anvil-cache"),
            "--silent"], stdout=node_log, stderr=node_log)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError(log_path.read_text())
                if w3.is_connected():
                    break
                time.sleep(.05)
            else:
                raise AssertionError("isolated Ethereum node did not start")
            assert w3.eth.chain_id == 1
            w3.provider.make_request("anvil_setBalance", [SENDER, hex(10**24)])
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            project = tmp_path / "runner-project"
            (project / "ops").mkdir(parents=True)
            (project / "web3").symlink_to(script_project(), target_is_directory=True)
            for name in ("deployment_stages.py", "ethereum_rpc_budget.py"):
                shutil.copyfile(ROOT / "ops" / name, project / "ops" / name)
            runner = project / "ops/deployment_stages.py"
            config_file, manifest_file = tmp_path / "config.json", tmp_path / "manifest.json"
            config = dict(chain_id="1", confirmations="12", parameters={
                "PROJECT_REGISTRY_OWNER": SENDER, "PROJECT_WALLET_1": SENDER,
                "PROJECT_WALLET_2": "0x" + "00" * 20, "PROJECT_WALLET_SHARES": "10000",
                "PROJECT_WALLET_LABELS": "Explicit Ethereum staging treasury",
            })
            environment = dict(os.environ, PRIVATE_KEY="0x" + "00" * 31 + "01",
                SATOSHIFI_MAX_FEE_PER_GAS_WEI="3000000000",
                SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI="1000000000",
                SATOSHIFI_MAX_TX_GAS=str(2**24))

            def stage(name, *, gas_limit=None, error=None, pending=False):
                config_file.write_text(json.dumps(config))
                command = [sys.executable, str(runner), "--config", str(config_file),
                    "--manifest", str(manifest_file), "--stage", name, "--rpc-url", rpc, "--broadcast"]
                local_env = dict(environment)
                if gas_limit is not None:
                    local_env["SATOSHIFI_MAX_TX_GAS"] = str(gas_limit)
                before = w3.eth.get_transaction_count(SENDER)
                result = subprocess.run(command, cwd=project, env=local_env,
                    capture_output=True, text=True, timeout=300)
                output = result.stdout + result.stderr
                (tmp_path / f"{name}-{gas_limit or 'full'}-{before}.log").write_text(output)
                assert environment["PRIVATE_KEY"][2:] not in output
                if error:
                    assert result.returncode != 0 and error in output, output
                    assert w3.eth.get_transaction_count(SENDER) == before
                    return None
                assert result.returncode == (2 if pending else 0), output
                state = json.loads(manifest_file.read_text())
                assert state["stages"][name]["confirmed"] is not pending, state
                return state

            # Neither a forbidden integration exemption nor an insufficient
            # actual signed-transaction budget may advance the deployer nonce.
            stage("exempt_all", error="ETHEREUM_MAINNET_EXEMPTION_FORBIDDEN")
            stage("base", gas_limit=21000, error="ETHEREUM_RPC_BUDGET_REJECTED")
            state = stage("base", pending=True)
            assert "inflight_run" in state
            assert any(item["status"] == "PENDING" for item in state["transactions"].values())
            nonce = w3.eth.get_transaction_count(SENDER)
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            state = stage("base")
            assert w3.eth.get_transaction_count(SENDER) == nonce
            assert "inflight_run" not in state
            assert "FROSTCoordinator" in state["addresses"]

            state = stage("factories", pending=True)
            nonce = w3.eth.get_transaction_count(SENDER)
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            state = stage("factories")
            assert w3.eth.get_transaction_count(SENDER) == nonce
            assert {"FederationFactory", "ConfederationFactory"} <= state["addresses"].keys()
            assert all(item["status"] == "CONFIRMED" for item in state["transactions"].values())
            evidence = []
            finalized = w3.eth.get_block("finalized").number
            for tx_hash, item in state["transactions"].items():
                transaction, receipt = w3.eth.get_transaction(tx_hash), w3.eth.get_transaction_receipt(tx_hash)
                assert receipt.status == 1 and receipt.blockNumber <= finalized
                assert transaction["type"] == 2 and transaction.chainId == 1
                assert transaction["from"] == SENDER and transaction.value == 0
                assert 0 < transaction.gas <= 2**24
                assert 0 <= transaction.maxPriorityFeePerGas <= 1_000_000_000
                assert transaction.maxPriorityFeePerGas <= transaction.maxFeePerGas <= 3_000_000_000
                evidence.append(dict(hash=tx_hash, contract=item["contract"], gas=transaction.gas,
                    gas_used=receipt.gasUsed, fee_paid_wei=str(receipt.gasUsed * receipt.effectiveGasPrice),
                    max_fee_per_gas=transaction.maxFeePerGas,
                    max_priority_fee_per_gas=transaction.maxPriorityFeePerGas,
                    block_number=receipt.blockNumber, sizes=item.get("sizes")))
            assert evidence and environment["PRIVATE_KEY"][2:] not in manifest_file.read_text()
            # Script/test gas is not deployment gas. Sum actual independent
            # receipts and compare every signed limit with the transaction cap.
            import importlib.util
            spec = importlib.util.spec_from_file_location("deployment_gas", ROOT / "ops/deployment_gas.py")
            gas_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gas_module)
            costs = gas_module.report(w3, state)
            assert costs["total_gas_used"] == sum(row["gas_used"] for row in evidence)
            assert costs["total_gas_used"] > costs["maximum_transaction_gas_used"]
            assert costs["maximum_transaction_gas_limit"] <= 2**24
            record_property("deployment_actual_gas", json.dumps(costs))
            record_property("ethereum_staged_scope", json.dumps(dict(chain_id=1, hardfork="osaka",
                real_forge_broadcasts=True, production_rpc_budget_proxy=True,
                finality="isolated Anvil finalized tag", live_mainnet_transactions=False,
                stages=["base", "factories"], transactions=evidence)))
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

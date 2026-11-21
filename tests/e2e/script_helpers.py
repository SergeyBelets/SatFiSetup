"""Run Foundry scripts with artificial key 1, without loading the operator's .env.

The temporary project contains copies of code/dependencies, its own broadcast
directory, and no .env. Artifacts are built before E2E starts.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from eth_account import Account
from web3 import Web3
from signer_agent.deployment_limits import check_deployment
from test_dkg_processes import artifact, RPC

ROOT = Path(__file__).resolve().parents[2]
SENDER = Account.from_key((1).to_bytes(32, "big")).address


_PROJECT = None


def script_project():
    global _PROJECT
    if _PROJECT is None:
        _PROJECT = tempfile.TemporaryDirectory(prefix="satohsi-forge-scripts-")
        project = Path(_PROJECT.name).resolve()
        original = ROOT / "web3"
        for name in ("contracts", "script", "lib", "test"):
            shutil.copytree(
                original / name,
                project / name,
                ignore=shutil.ignore_patterns(".git", ".DS_Store"),
            )
        shutil.copyfile(original / "foundry.toml", project / "foundry.toml")
        cache = project / "cache/solidity-files-cache.json"
        cache.parent.mkdir()
        cache.write_text(
            (original / "cache/solidity-files-cache.json")
            .read_text()
            .replace(str(original), str(project))
        )
    return Path(_PROJECT.name).resolve()


def run_script(w3, directory, name, env=None, sig=None, args=(), expect_failure=None):
    directory.mkdir(parents=True, exist_ok=True)
    project = script_project()
    w3.provider.make_request("anvil_setBalance", [SENDER, hex(10**24)])
    w3.provider.make_request("anvil_impersonateAccount", [SENDER])
    command = [
        "forge",
        "script",
        f"script/{name}.s.sol:{name}",
        "--root",
        str(project),
        "--rpc-url",
        RPC,
        "--broadcast",
        "--slow",
        "--non-interactive",
        "--sender",
        SENDER,
        "--unlocked",
    ]
    if sig:
        command += ["--sig", sig, *args]
    environment = dict(
        os.environ,
        PRIVATE_KEY="0x" + "00" * 31 + "01",
        EXPECTED_CHAIN_ID="31337",
        DEPLOYMENT_STAGE="exempt_all",
        PROJECT_REGISTRY_OWNER=SENDER,
        PROJECT_WALLET_SHARES="10000",
        PROJECT_WALLET_LABELS="Integration treasury",
        DAO_TREASURY=SENDER,
        INTEGRATION_FEE_EXEMPTION="true",
        DEPLOYMENT_FEE_ROUTER="0x" + "00" * 20,
        DEPLOYMENT_FEE_AMOUNT="0",
        PROJECT_WALLET_1=SENDER,
        PROJECT_WALLET_2="0x" + "00" * 20,
        DAO_LIQUIDITY_GUARD="0x" + "00" * 20,
        DAO_MINIMUM_LIQUIDITY="0",
    )
    environment.update(env or {})
    environment["FOUNDRY_BROADCAST"] = str(directory / "broadcast")
    nonce = w3.eth.get_transaction_count(SENDER)
    result = subprocess.run(
        command,
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    (directory / f"{name}.log").write_text(output)
    if expect_failure:
        assert result.returncode != 0 and expect_failure in output, output
        assert w3.eth.get_transaction_count(SENDER) == nonce
        return {}
    assert result.returncode == 0, output
    broadcast = (
        directory
        / "broadcast"
        / f"{name}.s.sol"
        / "31337"
        / ((sig.split("(")[0] if sig else "run") + "-latest.json")
    )
    if not broadcast.exists():
        assert w3.eth.get_transaction_count(SENDER) == nonce
        return {}  # Idempotent registration can produce no transactions.
    data = json.loads(broadcast.read_text())
    deployed, evidence = {}, []
    for item in data["transactions"]:
        receipt = w3.eth.get_transaction_receipt(item["hash"])
        assert receipt.status == 1
        if item["transactionType"] not in ("CREATE", "CREATE2"):
            continue
        address, contract_name = (
            Web3.to_checksum_address(item["contractAddress"]),
            item["contractName"],
        )
        transaction = w3.eth.get_transaction(item["hash"])
        assert (
            item["transactionType"] == "CREATE"
        ), "Add CREATE2 input decoding if introduced"
        sizes = check_deployment(w3.eth.get_code(address), transaction["input"])
        assert sizes["runtime_bytes"] > 0
        evidence.append(
            dict(contract=contract_name, address=address, tx=item["hash"], **sizes)
        )
        deployed[contract_name] = w3.eth.contract(
            address=address, abi=artifact(contract_name)["abi"]
        )
    (directory / "public-sizes.json").write_text(json.dumps(evidence, indent=2))
    return deployed

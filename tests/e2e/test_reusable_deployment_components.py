"""Real broadcasts with existing canonical components and a wrong-runtime preflight.

Only artificial local identities are used. The copied Foundry project has no
dotenv file. The project DKG uses the existing independent participant processes.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from web3 import Web3

from lifecycle_helpers import contract
from script_helpers import SENDER, script_project
from test_dkg_processes import RPC, ceremony, deploy

ROOT = Path(__file__).resolve().parents[2]


def test_existing_runtime_preflight_and_canonical_fee_router_resume(tmp_path, record_property):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337
    w3.provider.make_request("anvil_setBalance", [SENDER, hex(10**24)])
    project = tmp_path / "runner-project"
    (project / "ops").mkdir(parents=True)
    (project / "web3").symlink_to(script_project(), target_is_directory=True)
    runner = project / "ops/deployment_stages.py"
    for name in ("deployment_stages.py", "ethereum_rpc_budget.py"):
        shutil.copyfile(ROOT / "ops" / name, project / "ops" / name)
    config_file = tmp_path / "config.json"
    base_manifest, fee_manifest = tmp_path / "base.json", tmp_path / "fee.json"
    config = dict(chain_id="31337", confirmations="1", parameters={
        "PROJECT_REGISTRY_OWNER": SENDER, "PROJECT_WALLET_1": SENDER,
        "PROJECT_WALLET_2": "0x" + "00" * 20,
        "PROJECT_WALLET_SHARES": "10000", "PROJECT_WALLET_LABELS": "Explicit reused component test",
    })

    def stage(name, manifest, *, expected_error=None):
        config_file.write_text(json.dumps(config))
        nonce = w3.eth.get_transaction_count(SENDER)
        result = subprocess.run([sys.executable, str(runner), "--config", str(config_file),
            "--manifest", str(manifest), "--stage", name, "--rpc-url", RPC, "--broadcast"],
            cwd=project, env=dict(os.environ, PRIVATE_KEY="0x" + "00" * 31 + "01"),
            capture_output=True, text=True, timeout=300)
        output = result.stdout + result.stderr
        (tmp_path / (manifest.stem + "-" + name + "-latest.log")).write_text(output)
        if expected_error:
            assert result.returncode != 0 and expected_error in output, output
            assert w3.eth.get_transaction_count(SENDER) == nonce
        else:
            assert result.returncode == 0, output
        state = json.loads(manifest.read_text())
        if not expected_error:
            assert state["stages"][name]["confirmed"], state
        return state, w3.eth.get_transaction_count(SENDER) - nonce

    # Before the fix this consumed 28 deployment transactions, renounced ADMIN,
    # and confirmed a coordinator with ERC-20 code as its immutable verifier.
    wrong_verifier = deploy(w3, "DAOTestUSD")
    config["parameters"]["FROST_VERIFIER"] = wrong_verifier.address
    rejected, sent = stage("base", base_manifest, expected_error="DEPLOYED_CODE_MISMATCH: FROSTVerifier")
    assert sent == 0 and not rejected["runs"] and not rejected["addresses"]

    verifier = deploy(w3, "FROSTVerifier")
    config["parameters"]["FROST_VERIFIER"] = verifier.address
    base, sent = stage("base", base_manifest)
    assert sent > 0
    anchor = base["addresses"]["FROSTVerifier"]
    assert anchor["address"] == verifier.address
    assert anchor["provenance"] == "observed_existing" and "tx_hash" not in anchor
    assert w3.eth.get_block(anchor["block_number"])["hash"].hex().removeprefix("0x") == anchor["block_hash"].removeprefix("0x")
    original_addresses = dict(base["addresses"])
    base, sent = stage("base", base_manifest)
    assert sent == 0 and base["addresses"] == original_addresses

    registry = contract(w3, "DKGCoordinator", base["addresses"]["DKGCoordinator"]["address"])
    coordinator = contract(w3, "FROSTCoordinator", base["addresses"]["FROSTCoordinator"]["address"])
    leaf = base["addresses"]["FROSTAuthorizationDeployer"]["address"]
    assert coordinator.functions.defaultVerifier().call() == verifier.address
    assert not coordinator.functions.hasRole(Web3.keccak(text="ADMIN_ROLE"), SENDER).call()
    ceremony_dir = tmp_path / "project-dkg"
    ceremony_dir.mkdir()
    dkg, _, _ = ceremony(ceremony_dir, w3, registry, 3, 2, "EVM_AUTH")
    reward = deploy(w3, "DAOTestUSD")
    config["parameters"] = {
        "DKG_COORDINATOR": registry.address, "FROST_COORDINATOR": coordinator.address,
        "FROST_AUTHORIZATION_DEPLOYER": leaf, "PROJECT_DKG_ID": Web3.to_hex(dkg.session_id),
        "PROJECT_REWARD_TOKEN": reward.address, "PROJECT_TREASURY": w3.eth.accounts[9],
        "WRAP_STAKER_BPS": "2500", "DEPLOY_STAKER_BPS": "0",
    }
    # A new manifest legitimately starts from existing infrastructure. Its leaf
    # must be trusted before reading the internal authorization creation event.
    fee, sent = stage("fee_router", fee_manifest)
    assert sent == 3
    for name in ("DKGCoordinator", "FROSTCoordinator", "FROSTAuthorizationDeployer", "FROSTVerifier"):
        assert fee["addresses"][name]["provenance"] == "observed_existing"
    auth_anchor = fee["addresses"]["DKGAuthorization"]
    assert auth_anchor["tx_hash"] in fee["transactions"]
    auth = contract(w3, "DKGAuthorization", auth_anchor["address"])
    vault = contract(w3, "CustodianDAOIncomeVault", fee["addresses"]["CustodianDAOIncomeVault"]["address"])
    assert vault.functions.authorization().call() == auth.address
    assert fee["project_dkg_result"] == Web3.to_hex(auth.functions.resultHash().call())
    original_addresses = dict(fee["addresses"])
    repeated, sent = stage("fee_router", fee_manifest)
    assert sent == 0 and repeated["addresses"] == original_addresses
    record_property("reused_deployment_components", json.dumps(dict(
        wrong_runtime_transactions=0, wrong_runtime_rejected=True,
        base_observation=anchor, project_authorization=auth_anchor,
        project_dkg_result=fee["project_dkg_result"], router_resume_transactions=sent)))

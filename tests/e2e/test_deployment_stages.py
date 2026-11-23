"""Actual staged broadcasts, independent project FROST and both paid V1 factories.

All keys are artificial local identities; the copied Foundry project has no .env.
Manifest assertions refer to mined transactions, not a successful simulation exit.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from web3 import Web3
from signer_agent.bootstrap_formation import confederation_formation_hash, federation_formation_hash, share_token_hash

from lifecycle_helpers import contract
from script_helpers import SENDER, script_project
from test_bitcoin_processes import authorize
from test_dkg_processes import RPC, ceremony, deploy, tx

ROOT = Path(__file__).resolve().parents[2]


def test_staged_manifest_and_project_fee_with_real_frost(tmp_path, record_property):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337
    w3.provider.make_request("anvil_setBalance", [SENDER, hex(10**24)])
    w3.provider.make_request("anvil_impersonateAccount", [SENDER])
    project = tmp_path / "runner-project"
    (project / "ops").mkdir(parents=True)
    (project / "web3").symlink_to(script_project(), target_is_directory=True)
    runner = project / "ops/deployment_stages.py"
    for name in ("deployment_stages.py", "ethereum_rpc_budget.py"):
        shutil.copyfile(ROOT / "ops" / name, project / "ops" / name)
    config_file, manifest_file = tmp_path / "config.json", tmp_path / "manifest.json"
    config = dict(chain_id="31337", confirmations="1", parameters={
        "PROJECT_REGISTRY_OWNER": SENDER,
        "PROJECT_WALLET_1": SENDER,
        "PROJECT_WALLET_2": "0x" + "00" * 20,
        "PROJECT_WALLET_SHARES": "10000",
        "PROJECT_WALLET_LABELS": "Explicit staged test wallet",
    })

    def stage(name, *, broadcast=True, error=None, manifest=manifest_file,
              resume=False, interrupt_after_forge=False):
        config_file.write_text(json.dumps(config))
        command = [sys.executable, str(runner), "--config", str(config_file),
                   "--manifest", str(manifest), "--stage", name, "--rpc-url", RPC]
        if broadcast:
            command.append("--broadcast")
        if resume:
            command.append("--resume-broadcast")
        if interrupt_after_forge:
            # Test-only process driver: Forge really broadcasts and returns, then
            # the controller exits before importing its journal or any receipt.
            # No fault-injection switch exists in the production runner.
            driver = '''
import runpy
import sys
module = runpy.run_path(sys.argv[1], run_name="interrupted_stage_runner")
real_run = module["subprocess"].run
def interrupted_run(*args, **kwargs):
    completed = real_run(*args, **kwargs)
    print(module["redact"](completed.stdout + completed.stderr, sys.argv[sys.argv.index("--rpc-url") + 1]), end="")
    if completed.returncode:
        raise SystemExit(completed.returncode)
    raise SystemExit(86)
module["subprocess"].run = interrupted_run
raise SystemExit(module["main"](sys.argv[2:]))
'''
            command = [sys.executable, "-c", driver] + command[1:]
        environment = dict(os.environ, PRIVATE_KEY="0x" + "00" * 31 + "01")
        nonce = w3.eth.get_transaction_count(SENDER)
        result = subprocess.run(command, cwd=project, env=environment,
                                capture_output=True, text=True, timeout=300)
        output = result.stdout + result.stderr
        (tmp_path / (name + "-latest.log")).write_text(output)
        if interrupt_after_forge:
            assert result.returncode == 86, output
            assert w3.eth.get_transaction_count(SENDER) > nonce
            state = json.loads(manifest.read_text())
            assert state["inflight_run"] == state["stages"][name]["run_id"]
            assert not state["stages"][name]["confirmed"]
            return state
        if error:
            assert result.returncode != 0 and error in output, output
            assert w3.eth.get_transaction_count(SENDER) == nonce
            return
        assert result.returncode == 0, output
        state = json.loads(manifest.read_text())
        if broadcast:
            assert state["stages"][name]["confirmed"], state
        else:
            assert w3.eth.get_transaction_count(SENDER) == nonce
        return state

    # Missing explicit wallet data fails before any contract or transaction exists.
    wallet = config["parameters"].pop("PROJECT_WALLET_1")
    stage("base", error="PROJECT_WALLET_1", manifest=tmp_path / "missing.json")
    config["parameters"]["PROJECT_WALLET_1"] = wallet
    state = stage("base", interrupt_after_forge=True)
    interrupted_run = state["inflight_run"]
    assert not state["transactions"] and not state["addresses"]
    journal = tmp_path / "manifest-broadcast" / interrupted_run / "DeployFederation.s.sol/31337/run-latest.json"
    submitted = json.loads(journal.read_text())["transactions"]
    assert submitted and all(item["hash"] for item in submitted)
    nonce = w3.eth.get_transaction_count(SENDER)
    state = stage("base", resume=True)
    assert state["stages"]["base"]["run_id"] == interrupted_run
    assert "inflight_run" not in state
    assert w3.eth.get_transaction_count(SENDER) == nonce
    assert len(state["transactions"]) == len(submitted)
    assert "DEPLOYMENT_FEE_ROUTER" not in state["parameters"]
    assert "DEPLOYMENT_FEE_AMOUNT" not in state["parameters"]
    assert "FederationFactory" not in state["addresses"]
    base_addresses = dict(state["addresses"])
    nonce = w3.eth.get_transaction_count(SENDER)
    state = stage("base")
    assert state["addresses"] == base_addresses
    assert w3.eth.get_transaction_count(SENDER) == nonce

    def bound(name):
        return contract(w3, name, state["addresses"][name]["address"])

    registry, coordinator = bound("DKGCoordinator"), bound("FROSTCoordinator")
    assert coordinator.functions.dkgRegistry().call() == registry.address
    assert not coordinator.functions.hasRole(Web3.keccak(text="ADMIN_ROLE"), SENDER).call()
    state = stage("factories")
    factory, conf_factory = bound("FederationFactory"), bound("ConfederationFactory")
    assert not factory.functions.deploymentFeeConfigured().call()
    assert not conf_factory.functions.deploymentFeeConfigured().call()
    nonce = w3.eth.get_transaction_count(SENDER)
    stage("factories")
    assert w3.eth.get_transaction_count(SENDER) == nonce

    project_dir = tmp_path / "project-dkg"
    project_dir.mkdir()
    project_dkg, project_participant, _ = ceremony(project_dir, w3, registry, 3, 2, "EVM_AUTH")
    usd = deploy(w3, "DAOTestUSD")
    treasury, fee = w3.eth.accounts[9], 100_000
    config["parameters"].update({
        "PROJECT_DKG_ID": Web3.to_hex(project_dkg.session_id),
        "PROJECT_REWARD_TOKEN": usd.address,
        "PROJECT_TREASURY": treasury,
        "WRAP_STAKER_BPS": "2500",
        "DEPLOY_STAKER_BPS": "0",
        "INTEGRATION_FEE_EXEMPTION": "false",
        "DEPLOYMENT_FEE_AMOUNT": str(fee),
    })
    state = stage("fee_router")
    auth, vault, router = bound("DKGAuthorization"), bound("CustodianDAOIncomeVault"), bound("CustodianDAOFeeRouter")
    assert auth.functions.dkgId().call() == project_dkg.session_id
    assert state["project_dkg_result"] == Web3.to_hex(auth.functions.resultHash().call())
    assert router.functions.deployStakerBps().call() == 0
    assert int(vault.functions.settlement().call(), 16) == 0
    assert not vault.functions.sources(vault.functions.DEPLOY_FEE().call()).call()[2]
    nonce = w3.eth.get_transaction_count(SENDER)
    stage("fee_router")
    assert w3.eth.get_transaction_count(SENDER) == nonce
    stage("fees", error="payer permissions missing")

    hashes = []
    for index, payer in enumerate((factory.address, conf_factory.address)):
        stage("prepare_payer", broadcast=False)
        current_nonce = router.functions.operationNonce().call()
        assert current_nonce == index
        message = authorize(w3, coordinator, auth, project_participant, tmp_path,
                            router.address, "CustodianDAOFeeRouter",
                            "getSetPayerOperationHash", [payer, True],
                            approve_onchain=bool(index))
        # First execution performs authorization itself. Second resumes a real
        # already confirmed approval, then consumes it in the payer transaction.
        config["parameters"].update({
            "PAYER_OPERATION_NONCE": str(current_nonce),
            "PAYER_OPERATION_HASH": Web3.to_hex(message),
            "FROST_SESSION_ID": str(coordinator.functions.nextSessionId().call()),
        })
        state = stage("execute_payer")
        assert router.functions.authorizedPayers(payer).call()
        assert router.functions.operationNonce().call() == index + 1
        hashes.append(message)
        if index == 0:
            stage("execute_payer", error="stale payer operation")
    assert hashes[0] != hashes[1]
    state = stage("fees")
    nonce = w3.eth.get_transaction_count(SENDER)
    stage("fees")
    assert w3.eth.get_transaction_count(SENDER) == nonce

    # Independent user ceremonies remain separate from project authorization.
    fed_dir = tmp_path / "federation-dkg"
    fed_dir.mkdir()
    fed, _, _ = ceremony(fed_dir, w3, registry, 3, 2, "EVM_AUTH")
    assert fed.session_id != project_dkg.session_id
    fed_metadata = ("Staged Federation", "SF")
    tx(w3, factory.functions.reserveFederationId(fed.session_id, "staged-paid-fed",
        federation_formation_hash(fed.participants, fed.threshold), share_token_hash(*fed_metadata)), sender=SENDER)
    receipt = w3.eth.wait_for_transaction_receipt(factory.functions.adoptFederationDKG(
        fed.session_id, "staged-paid-fed").transact({"from": SENDER}))
    assert receipt.status == 1

    def paid_deploy(target, method, args, active):
        payer = SENDER
        before = usd.functions.balanceOf(treasury).call()
        failed = w3.eth.wait_for_transaction_receipt(getattr(target.functions, method)(*args).transact(
            {"from": payer, "gas": 16_000_000}))
        assert failed.status == 0
        assert not active()
        assert usd.functions.balanceOf(treasury).call() == before
        tx(w3, usd.functions.mint(payer, fee))
        tx(w3, usd.functions.approve(target.address, fee), sender=payer)
        balance = usd.functions.balanceOf(payer).call()
        tx(w3, getattr(target.functions, method)(*args), sender=payer)
        assert active()
        assert usd.functions.balanceOf(payer).call() == balance - fee
        assert usd.functions.balanceOf(treasury).call() == before + fee
        assert usd.functions.balanceOf(router.address).call() == 0
        assert usd.functions.balanceOf(vault.address).call() == 0

    paid_deploy(factory, "deployFederation", [fed.session_id, *fed_metadata],
                lambda: factory.functions.isFederationActive("staged-paid-fed").call())
    conf_dir = tmp_path / "confederation-dkg"
    conf_dir.mkdir()
    conf, _, _ = ceremony(conf_dir, w3, registry, 3, 3, "EVM_AUTH")
    custodians = [("C2", conf.participants[1], b"\x02", True),
                  ("C3", conf.participants[2], b"\x02", True)]
    conf_metadata = ("Staged Confederation", "SC")
    tx(w3, conf_factory.functions.reserveConfederationId(conf.session_id, "staged-paid-conf",
        confederation_formation_hash(0, ["staged-paid-fed"], custodians, conf.threshold, 2),
        share_token_hash(*conf_metadata)), sender=SENDER)
    receipt = w3.eth.wait_for_transaction_receipt(conf_factory.functions.adoptConfederationDKG(
        conf.session_id, ["staged-paid-fed"], custodians, 3, 2, "staged-paid-conf"
    ).transact({"from": SENDER}))
    assert receipt.status == 1
    paid_deploy(conf_factory, "deployConfederation", [conf.session_id, *conf_metadata],
                lambda: conf_factory.functions.isConfederationActive("staged-paid-conf").call())
    assert all(item["status"] == "CONFIRMED" for item in state["transactions"].values())
    record_property("staged_public_manifest", json.dumps(state))

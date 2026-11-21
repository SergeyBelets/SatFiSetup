"""A missing participant and an EVM fork must not silently restart a secret ceremony."""

import json
import os
import subprocess
import sys
from eth_account import Account
from web3 import Web3
from signer_agent.bootstrap import Bootstrap
from signer_agent.dkg_protocol import Manifest
from test_dkg_processes import RPC, authorize_ceremony, deploy, cli


def test_first_dkg_timeout_and_removed_anchor_fail_closed(tmp_path):
    w3 = Web3(Web3.HTTPProvider(RPC))
    registry = deploy(w3, "DKGCoordinator")
    addresses = [Account.from_key(i.to_bytes(32, "big")).address for i in (41, 42)]
    keys = []
    for i, address in enumerate(addresses):
        w3.provider.make_request("anvil_setBalance", [address, hex(10**22)])
        directory = tmp_path / str(i)
        directory.mkdir()
        (directory / "identity.json").write_text(
            json.dumps({"private_key": "%064x" % (i + 41)})
        )
        (directory / "identity.json").chmod(0o600)
        (directory / "password").write_text(os.urandom(32).hex())
        (directory / "password").chmod(0o600)
        keys.append(
            bytes.fromhex(
                cli(
                    "transport-init",
                    "--state",
                    directory / "state.enc",
                    "--password-file",
                    directory / "password",
                )["public"]
            )
        )
    manifest = Manifest(
        31337,
        registry.address,
        os.urandom(32),
        1,
        "EVM_AUTH",
        tuple(addresses),
        tuple(keys),
        2,
    )
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest.to_dict()))
    base = [
        "--manifest",
        path,
        "--rpc",
        RPC,
        "--identity-file",
        tmp_path / "0/identity.json",
        "--state",
        tmp_path / "0/state.enc",
        "--password-file",
        tmp_path / "0/password",
        "--mailbox",
        tmp_path / "mailbox.sqlite",
    ]
    authorize_ceremony(w3, registry, manifest.session_id, addresses[0])
    cli("create", *base)
    snapshot = w3.provider.make_request("evm_snapshot", [])["result"]
    assert cli("run-once", *base)["status"] == "ROUND1_PUBLISHED"
    assert w3.provider.make_request("evm_revert", [snapshot])["result"]
    # A new branch with a different block at the recorded height cannot reuse the state.
    w3.provider.make_request("evm_increaseTime", [17])
    w3.provider.make_request("evm_mine", [])
    result = subprocess.run(
        [sys.executable, "-m", "signer_agent.cli", "dkg", "run-once", *map(str, base)],
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode == 2
        and json.loads(result.stdout)["code"] == "DKG_ANCHOR_REORG"
    )
    assert registry.functions.getSessionInfo(manifest.session_id).call()[4] == 1
    profile = dict(
        deployment_id="timeout",
        mode="integration",
        chain_id="31337",
        bitcoin_network="regtest",
        evm_rpc=RPC,
        contracts={"dkg": dict(address=registry.address, artifact="DKGCoordinator")},
        steps=[
            dict(
                id="ceremony",
                kind="wait",
                contract="dkg",
                method="getCompletedResult",
                args=["0x" + manifest.session_id.hex()],
                index=["3"],
                equals="0x" + Web3.keccak(text="EVM_AUTH").hex(),
            )
        ],
    )
    runner = Bootstrap(profile, tmp_path / "bootstrap.sqlite")
    assert runner.apply()["status"] == "AWAITING_PARTICIPANTS"
    w3.provider.make_request("evm_increaseTime", [3601])
    w3.provider.make_request("evm_mine", [])
    result = runner.apply()
    assert (
        result["status"] == "RECOVERY_REQUIRED"
        and result["reason"] == "CEREMONY_TIMEOUT"
    )
    assert registry.functions.getSessionInfo(manifest.session_id).call()[9] is False

"""Real HTTP preparation -> separate EVM executor -> canonical reads and retry."""

import hashlib
import json
import os
import threading
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import pytest
from eth_account import Account
from web3 import Web3
from signer_agent.bootstrap import Bootstrap
from signer_agent.operations import Journal, OperationJournal
from signer_agent.operational_api import OperationalHTTPServer, handler
from signer_agent.operation_worker import execution_lock, main as execute
from test_dkg_processes import RPC


@pytest.mark.parametrize("legacy", [False, True], ids=["scoped", "legacy"])
def test_api_execution_changes_balance_once_and_remains_ready(tmp_path, legacy):
    w3 = Web3(Web3.HTTPProvider(RPC))
    account = Account.create()
    recipient = Account.create().address
    w3.provider.make_request("anvil_setBalance", [account.address, hex(10**22)])
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"private_key": account.key.hex()}))
    os.chmod(identity, 0o600)
    profile = dict(
        deployment_id="api-e2e",
        mode="integration",
        chain_id="31337",
        bitcoin_network="regtest",
        evm_rpc=RPC,
        steps=[
            dict(
                id="token",
                kind="deploy",
                artifact="CustodianDAOToken",
                args=["Integration Governance", "IGOV", account.address],
            )
        ],
    )
    state = tmp_path / "ops.sqlite"
    bootstrap = Bootstrap(profile, state)
    assert bootstrap.apply(identity)["status"] == "VERIFIED"
    bearer = os.urandom(32).hex()
    policy = dict(
        principals=[
            dict(sha256=hashlib.sha256(bearer.encode()).hexdigest(), role="operator")
        ],
        deployments=["api-e2e"],
        actions={
            "send": dict(
                deployment="api-e2e",
                contract="token",
                artifact="CustodianDAOToken",
                method="transfer",
            )
        },
        views={
            "received": dict(
                deployment="api-e2e",
                contract="token",
                method="balanceOf",
                args=[recipient],
                units="IGOV base units, decimals=18",
            )
        },
    )
    policy["views"]["balance"] = dict(policy["views"]["received"], parameters=True)
    policy["metrics"] = {"recipient_balance": {"view": "received"}}
    policy_path = tmp_path / "policy.json"
    params_path = tmp_path / "params.json"
    policy_path.write_text(json.dumps(policy))
    journal = Journal(state)
    amount = "9007199254740993000"
    payload = dict(action="send", args=[recipient, amount])
    if legacy:
        spec = policy["actions"]["send"]
        contracts = bootstrap.apply(verify=True)["manifest"]["contracts"]
        journal.put("transfer-once", dict(payload, spec=spec))
        old_profile = dict(profile, deployment_id="operation-transfer-once", contracts=contracts, steps=[
            dict(id="execute", kind="call", contract=spec["contract"], method=spec["method"], args=payload["args"])
        ])
        Bootstrap(old_profile, state)
    server = OperationalHTTPServer(("127.0.0.1", 0), handler(policy, journal))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def http(path, body=None):
        req = Request(
            "http://127.0.0.1:" + str(server.server_port) + path,
            headers={
                "Authorization": "Bearer " + bearer,
                "Idempotency-Key": "transfer-once",
            },
            data=None if body is None else json.dumps(body).encode(),
        )
        with urlopen(req) as response:
            return response.status, json.load(response)

    try:
        assert http("/readiness")[1]["status"] == "READY"
        params_path.write_text(json.dumps(payload))
        assert (
            execute(
                [
                    "prepare",
                    "--id",
                    "transfer-once",
                    "--state",
                    str(state),
                    "--policy",
                    str(policy_path),
                    "--params",
                    str(params_path),
                ]
            )
            == 0
        )
        assert http("/v1/operations", payload)[0] == 202
        assert (
            http("/v1/actions")[1]["actions"]["send"]["inputs"][1]["type"] == "uint256"
        )
        assert http("/v1/deployments/api-e2e")[1]["status"] == "VERIFIED"
        args = [
            "execute",
            "--id",
            "transfer-once",
            "--state",
            str(state),
            "--identity-file",
            str(identity),
        ]
        before = w3.eth.get_transaction_count(account.address)
        with pytest.raises(ValueError, match="^API_EXECUTION_POLICY_REQUIRED$"):
            execute(args)
        policy_path.write_text(json.dumps(dict(policy, actions={})))
        with pytest.raises(ValueError, match="^ACTION_NOT_ALLOWED$"):
            execute(args + ["--policy", str(policy_path)])
        assert w3.eth.get_transaction_count(account.address) == before
        policy_path.write_text(json.dumps(policy))
        args += ["--policy", str(policy_path)]
        assert execute(args + ["--dry-run", "--sender", account.address]) == 0
        assert w3.eth.get_transaction_count(account.address) == before
        operation = OperationJournal(state).find("transfer-once", ["api-e2e"])
        with execution_lock(Journal(state), operation):
            with pytest.raises(ValueError, match="API_OPERATION_EXECUTION_IN_PROGRESS"):
                execute(args)
        assert http("/v1/operations/transfer-once")[1]["status"] == "PREPARED"
        assert w3.eth.get_transaction_count(account.address) == before
        assert execute(args) == 0
        nonce = w3.eth.get_transaction_count(account.address)
        assert execute([
            "receipt", "--deployment", "api-e2e", "--id", "transfer-once",
            "--state", str(state), "--rpc", RPC,
        ]) == 0
        assert w3.eth.get_transaction_count(account.address) == nonce
        assert http("/v1/views/received")[1]["value"] == amount
        query = urlencode({"args": json.dumps([recipient])})
        assert http("/v1/views/balance?" + query)[1]["value"] == amount
        assert http("/metrics")[1]["onchain"]["recipient_balance"]["value"] == amount
        assert http("/readiness")[1]["status"] == "READY"
        assert http("/v1/operations", payload)[1]["status"] == "VERIFIED"
        # Revocation denies a fresh signing attempt, while the exact settled
        # request still reconciles from its existing transaction record.
        policy_path.write_text(json.dumps(dict(policy, actions={})))
        assert execute(args) == 0
        assert w3.eth.get_transaction_count(account.address) == nonce
        assert http("/v1/views/received")[1]["value"] == amount
        public = json.dumps(http("/v1/operations")[1])
        assert bearer not in public and account.key.hex() not in public
        assert "private_key" not in public
        assert "raw_transaction" not in public and "evm_rpc" not in public
        assert [row["id"] for row in http("/v1/operations")[1]["operations"]] == [
            "transfer-once"
        ]
        assert "raw_transaction" not in json.dumps(http("/v1/deployments/api-e2e")[1])
        execution_state = state.with_name(state.name + ".api-execution.sqlite")
        if legacy:
            assert journal.get("operation-transfer-once:execute")["status"] == "CONFIRMED"
            assert not execution_state.exists()
        else:
            execution_state.rename(tmp_path / "missing-from-operator-state.sqlite")
            with pytest.raises(ValueError, match="API_OPERATION_EXECUTION_JOURNAL_MISSING"):
                execute(args)
            assert http("/v1/operations/transfer-once")[1]["status"] == "VERIFIED"
            assert http("/v1/views/received")[1]["value"] == amount
            assert w3.eth.get_transaction_count(account.address) == nonce
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("previous_build", [False, True])
def test_verified_evm_exit_remains_independent_of_unavailable_bitcoin_rpc(tmp_path, monkeypatch, previous_build):
    from signer_agent import bitcoin_trust, bootstrap as bootstrap_module, operational_api

    w3 = Web3(Web3.HTTPProvider(RPC))
    owner = Account.create()
    w3.provider.make_request("anvil_setBalance", [owner.address, hex(10**22)])
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"private_key": owner.key.hex()}))
    os.chmod(identity, 0o600)
    deployment = "api-backed-exit"
    profile = dict(
        deployment_id=deployment, mode="backed", chain_id="31337",
        bitcoin_network="testnet4", bitcoin_policy={"backend": "relay"}, evm_rpc=RPC,
        steps=[
            dict(id="ulr", kind="deploy", artifact="CustodianDAOToken",
                 args=["Stake", "STK", owner.address]),
            dict(id="reward", kind="deploy", artifact="CustodianDAOToken",
                 args=["Reward", "RWD", owner.address]),
            dict(id="staking", kind="deploy", artifact="ULRStaking", args=["$ulr", "$reward", "1", "1"],
                 checks=[dict(contract="staking", method="stakeToken", args=[], equals="$ulr")]),
            dict(id="approve", kind="call", contract="ulr", method="approve", args=["$staking", "100"]),
            dict(id="stake", kind="call", contract="staking", method="stake", args=["100"]),
            dict(id="bitcoin_ready", kind="bitcoin_ready"),
        ],
    )
    state = tmp_path / "ops.sqlite"
    bootstrap = Bootstrap(profile, state)
    # This fixture uses real EVM deployments and an already reviewed Bitcoin barrier.
    monkeypatch.setattr(bootstrap, "bitcoin_ready",
                        lambda *_args, **_kwargs: {"status": "MAINNET_RELAY_READY"})
    current_artifact = bootstrap_module.artifact
    if previous_build:
        archived = bitcoin_trust.previous_artifact("ULRStaking")
        with monkeypatch.context() as old_build:
            def artifact_for_deployment(kind):
                return archived if kind == "ULRStaking" else current_artifact(kind)

            old_build.setattr(bootstrap_module, "artifact", artifact_for_deployment)
            old_build.setattr(bitcoin_trust, "artifact", artifact_for_deployment)
            assert bootstrap.apply(identity)["status"] == "VERIFIED"
        assert bootstrap.journal.get(deployment + ":staking")["result"]["contract"]["artifact_hash"] == (
            bootstrap_module.artifact_fingerprint(archived))
    else:
        assert bootstrap.apply(identity)["status"] == "VERIFIED"
    staking = bootstrap.contract("staking")
    assert staking.functions.stakedBalance(owner.address).call() == 100
    spec = dict(deployment=deployment, contract="staking", artifact="ULRStaking")
    actions = {"exit": dict(spec, method="unstake"), "other": dict(spec, method="stake")}
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"deployments": [deployment], "actions": actions}))
    requests = OperationJournal(state)
    for key in actions:
        requests.put(key, dict(action=key, args=["1"], spec=actions[key]))
    journal = Journal(state)
    saved_deployment = journal.get(deployment)
    saved_bitcoin = journal.get(deployment + ":bitcoin_ready")

    def reject_bitcoin_connection(*_args, **_kwargs):
        raise AssertionError("Bitcoin RPC contact by EVM-only exit")

    monkeypatch.setattr(operational_api, "BitcoinRPC", reject_bitcoin_connection)
    args = ["--state", str(state), "--policy", str(policy), "--identity-file", str(identity)]
    before = w3.eth.get_transaction_count(owner.address)
    with pytest.raises(ValueError, match="^MAINNET_CORE_REQUIRED$"):
        execute(["execute", "--id", "other", *args])
    assert execute(["execute", "--id", "exit", *args]) == 0
    assert execute(["execute", "--id", "exit", *args]) == 0
    assert w3.eth.get_transaction_count(owner.address) == before + 1
    assert staking.functions.stakedBalance(owner.address).call() == 99
    assert journal.get(deployment) == saved_deployment
    assert journal.get(deployment + ":bitcoin_ready") == saved_bitcoin

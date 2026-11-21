"""Real HTTP preparation -> separate EVM executor -> canonical reads and retry."""

import hashlib
import json
import os
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import pytest
from eth_account import Account
from web3 import Web3
from signer_agent.bootstrap import Bootstrap
from signer_agent.operations import Journal, OperationJournal
from signer_agent.operational_api import handler
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(policy, journal))
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
        assert http("/v1/views/received")[1]["value"] == amount
        query = urlencode({"args": json.dumps([recipient])})
        assert http("/v1/views/balance?" + query)[1]["value"] == amount
        assert http("/metrics")[1]["onchain"]["recipient_balance"]["value"] == amount
        assert http("/readiness")[1]["status"] == "READY"
        assert http("/v1/operations", payload)[1]["status"] == "VERIFIED"
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

import json, os
import pytest
from eth_account import Account
from web3 import Web3
from signer_agent.bootstrap import Bootstrap
from test_dkg_processes import RPC


def test_crash_before_broadcast_resumes_same_contract_and_supply(tmp_path, monkeypatch):
    w3 = Web3(Web3.HTTPProvider(RPC))
    account = Account.create()
    w3.provider.make_request("anvil_setBalance", [account.address, hex(10**22)])
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"private_key": account.key.hex()}))
    os.chmod(identity, 0o600)
    profile = dict(
        deployment_id="resume-test",
        mode="integration",
        chain_id="31337",
        bitcoin_network="regtest",
        evm_rpc=RPC,
        steps=[
            dict(
                id="governance_token",
                kind="deploy",
                artifact="CustodianDAOToken",
                args=["Custodian Governance", "CGOV", account.address],
                checks=[
                    dict(
                        contract="governance_token",
                        method="totalSupply",
                        args=[],
                        equals=str(10**25),
                    )
                ],
            )
        ],
    )
    state = tmp_path / "state.sqlite"
    b = Bootstrap(profile, state)
    send = b.w3.eth.send_raw_transaction

    def crash(raw):
        raise RuntimeError("injected crash after signed journal")

    monkeypatch.setattr(b.w3.eth, "send_raw_transaction", crash)
    with pytest.raises(RuntimeError):
        b.apply(identity)
    entry = b.journal.get("resume-test:governance_token")
    assert entry["status"] == "SIGNED"
    original_hash = entry["result"]["tx_hash"]
    b = Bootstrap(profile, state)
    first = b.apply(identity)
    second = b.apply(identity)
    assert first["status"] == second["status"] == "VERIFIED"
    assert first["manifest"]["contracts"] == second["manifest"]["contracts"]
    assert (
        b.journal.get("resume-test:governance_token")["result"]["tx_hash"]
        == original_hash
    )
    assert w3.eth.get_transaction_count(account.address) == 1
    assert b.apply(verify=True)["status"] == "VERIFIED"
    from signer_agent import bootstrap as sdk
    artifact = sdk.artifact

    def rebuilt(name):
        result = artifact(name)
        if name == "CustodianDAOToken":
            result["bytecode"]["sourceMap"] = "equivalent rebuild"
        return result

    monkeypatch.setattr(sdk, "artifact", rebuilt)
    assert Bootstrap(profile, state).apply(verify=True)["status"] == "VERIFIED"
    assert w3.eth.get_transaction_count(account.address) == 1


def test_indexer_rewinds_orphaned_logs_without_money_actions(tmp_path):
    from signer_agent.operations import Journal
    from signer_agent.indexer import Indexer
    from test_dkg_processes import deploy, tx

    w3 = Web3(Web3.HTTPProvider(RPC))
    token = deploy(
        w3, "CustodianDAOToken", "Custodian Governance", "CGOV", w3.eth.accounts[0]
    )
    start = w3.eth.block_number
    snapshot = w3.provider.make_request("evm_snapshot", [])["result"]
    receipt = tx(w3, token.functions.transfer(w3.eth.accounts[1], 123))
    journal = Journal(tmp_path / "events.sqlite")
    index = Indexer(w3, journal, "31337")
    assert index.poll([token.address], start, 1)["status"] == "INDEXED"
    with journal.db() as c:
        assert (
            c.execute("SELECT count(*) FROM indexed_logs WHERE canonical=1").fetchone()[
                0
            ]
            == 2
        )
    assert w3.provider.make_request("evm_revert", [snapshot])["result"]
    tx(w3, token.functions.transfer(w3.eth.accounts[2], 456))
    assert index.poll([token.address], start, 1)["status"] == "RECOVERY_REQUIRED"
    with journal.db() as c:
        assert (
            c.execute("SELECT count(*) FROM indexed_logs WHERE canonical=0").fetchone()[
                0
            ]
            == 1
        )
        assert (
            c.execute("SELECT count(*) FROM indexed_logs WHERE canonical=1").fetchone()[
                0
            ]
            == 2
        )
    assert token.functions.balanceOf(w3.eth.accounts[1]).call() == 0
    assert token.functions.balanceOf(w3.eth.accounts[2]).call() == 456

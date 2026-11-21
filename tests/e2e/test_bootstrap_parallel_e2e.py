"""Real independent deployments and DKG creation behind finalized barriers."""
from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import socket
import subprocess
import time

from eth_account import Account
import pytest
from web3 import Web3

from signer_agent.bootstrap import Bootstrap
from signer_agent.operations import Journal
from test_dkg_processes import deploy, tx


@pytest.mark.parametrize("chain_id", [1, 11155111, 31337])
def test_bootstrap_groups_finalize_before_dependent_reads_and_share_nonce_lock(tmp_path, monkeypatch, chain_id, record_property):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    rpc = f"http://127.0.0.1:{port}"
    w3 = Web3(Web3.HTTPProvider(rpc))
    with (tmp_path / "node.log").open("w") as log:
        process = subprocess.Popen([shutil.which("anvil"), "--host", "127.0.0.1", "--port", str(port),
            "--chain-id", str(chain_id), "--hardfork", "osaka", "--gas-limit", "30000000", "--silent"], stdout=log, stderr=log)
        try:
            for _ in range(100):
                if w3.is_connected():
                    break
                if process.poll() is not None:
                    raise AssertionError((tmp_path / "node.log").read_text())
                time.sleep(.05)
            else:
                raise AssertionError("isolated node did not start")
            key = "0x" + "00" * 31 + "01"
            sender = Account.from_key(key).address
            identity = tmp_path / "identity.json"
            identity.write_text(json.dumps(dict(private_key=key)))
            identity.chmod(0o600)
            w3.provider.make_request("anvil_setBalance", [sender, hex(10**24)])
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            for name, value in dict(SATOSHIFI_MAX_FEE_PER_GAS_WEI="3000000000",
                SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI="1000000000", SATOSHIFI_MAX_TX_GAS=str(2**24)).items():
                monkeypatch.setenv(name, value)
            profile = dict(deployment_id="parallel-real", mode="backed" if chain_id == 1 else "integration",
                bitcoin_network="regtest", chain_id=str(chain_id), evm_rpc=rpc, evm_confirmations="12", steps=[
                    dict(id="dkg", kind="deploy", artifact="DKGCoordinator", args=[], parallel_group="leaf-deploys"),
                    dict(id="verifier", kind="deploy", artifact="FROSTVerifier", args=[], parallel_group="leaf-deploys")])
            protocol = "0x" + Web3.keccak(text="SATOHSI_DKG_FROST_SECP256K1_TR_2_2_0_POSTCARD_HPKE_X25519_CHACHA_V1").hex()
            sessions = ["0x" + f"{i:064x}" for i in (1, 2, 3)]
            for i, session in enumerate(sessions):
                profile["steps"].append(dict(id=f"ceremony{i}", kind="call", contract="dkg", method="createCeremony",
                    args=[session, [sender, w3.eth.accounts[2]], "2", ["0x" + "11" * 32, "0x" + "22" * 32], "1",
                          "0x" + Web3.keccak(text="BTC_SPEND" if i == 2 else "EVM_AUTH").hex(), protocol],
                    parallel_group="create-ceremonies"))
            profile["steps"].append(dict(id="after-all-ceremonies", kind="read", outputs=[dict(name=f"phase{i}",
                contract="dkg", method="getSessionInfo", args=[session], index=["4"]) for i, session in enumerate(sessions)]))
            state = tmp_path / "bootstrap.sqlite"
            b = Bootstrap(profile, state)
            assert b.apply(identity)["status"] in ("BROADCAST", "CONFIRMING")
            assert w3.eth.get_transaction_count(sender, "pending") == 2
            assert b.contracts == b.values == {}
            journal = Journal(state)
            hashes = [journal.get("parallel-real:" + name)["result"]["tx_hash"] for name in ("dkg", "verifier")]
            for tx_hash in hashes:
                assert w3.eth.wait_for_transaction_receipt(tx_hash, timeout=10).status == 1
            assert Bootstrap(profile, state).apply(identity)["status"] == "CONFIRMING"
            assert w3.eth.get_transaction_count(sender) == 2
            # Mere depth must not bypass Ethereum finality.
            w3.provider.make_request("anvil_mine", ["0xc", "0x0"])
            if chain_id == 1:
                assert b.apply(identity)["status"] == "CONFIRMING"
                assert w3.eth.get_transaction_count(sender) == 2
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            result = Bootstrap(profile, state).apply(identity)
            assert result["status"] in ("BROADCAST", "CONFIRMING"), result
            assert result["parallel_group"] == "create-ceremonies"
            assert w3.eth.get_transaction_count(sender, "pending") == 5
            assert all(journal.get("parallel-real:" + name)["status"] == "CONFIRMED" for name in ("dkg", "verifier"))
            with pytest.raises(KeyError):
                journal.get("parallel-real:after-all-ceremonies")
            for i in range(3):
                entry = journal.get(f"parallel-real:ceremony{i}")
                assert entry["status"] in ("BROADCAST", "CONFIRMING")
                hashes.append(entry["result"]["tx_hash"])
                assert w3.eth.wait_for_transaction_receipt(entry["result"]["tx_hash"], timeout=10).status == 1
            assert Bootstrap(profile, state).apply(identity)["status"] == "CONFIRMING"
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            done = Bootstrap(profile, state).apply(identity)
            assert done["status"] == "VERIFIED"
            assert done["manifest"]["values"] == {f"phase{i}": "1" for i in range(3)}
            assert Bootstrap(profile, state).apply(verify=True)["status"] == "VERIFIED"
            assert Bootstrap(profile, state).apply(identity)["status"] == "VERIFIED"
            assert w3.eth.get_transaction_count(sender) == 5
            assert [w3.eth.get_transaction(tx).nonce for tx in hashes] == list(range(5))
            if chain_id == 1:
                assert all(w3.eth.get_transaction(tx).type == 2 for tx in hashes)
            # Separate journals/instances sharing the identity must allocate
            # disjoint nonces even when execution overlaps in two threads.
            def other(number):
                p = dict(profile, deployment_id=f"concurrent-{number}", steps=[
                    dict(id=f"copy{i}", kind="deploy", artifact="FROSTVerifier", args=[], parallel_group="leaves") for i in range(2)])
                return Bootstrap(p, tmp_path / f"other-{number}.sqlite").apply(identity)
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(other, [1, 2]))
            assert all(result["status"] in ("BROADCAST", "CONFIRMING") for result in results), results
            assert w3.eth.get_transaction_count(sender, "pending") == 9
            concurrent = [Journal(tmp_path / f"other-{n}.sqlite").get(f"concurrent-{n}:copy{i}")["result"]
                for n in (1, 2) for i in range(2)]
            assert sorted(int(entry["nonce"]) for entry in concurrent) == [5, 6, 7, 8]
            for entry in concurrent:
                assert w3.eth.wait_for_transaction_receipt(entry["tx_hash"], timeout=10).status == 1
            assert w3.eth.get_transaction_count(sender) == 9
            # Another participant's mutable approval is an external observation,
            # so our own transaction receipt barriers cannot confer its depth.
            token = deploy(w3, "StageRewardToken")
            owner, spender = w3.eth.accounts[:2]
            w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
            before_approval = w3.provider.make_request("evm_snapshot", [])["result"]
            depth = "12" if chain_id == 1 else "3"
            approval_profile = dict(profile, deployment_id="external-approval", evm_confirmations=depth,
                contracts=dict(token=dict(address=token.address, artifact="StageRewardToken")),
                steps=[dict(id="approved", kind="wait", contract="token", method="allowance",
                    args=[owner, spender], equals="7", outputs=[dict(name="observed_allowance",
                    contract="token", method="allowance", args=[owner, spender])])])
            approval_state = tmp_path / "approval.sqlite"
            tx(w3, token.functions.approve(spender, 7))
            assert Bootstrap(approval_profile, approval_state).apply()["status"] == "AWAITING_PARTICIPANTS"
            if chain_id != 1:
                w3.provider.make_request("anvil_mine", ["0x1", "0x0"])
                assert Bootstrap(approval_profile, approval_state).apply()["status"] == "AWAITING_PARTICIPANTS"
            w3.provider.make_request("anvil_mine", ["0x80" if chain_id == 1 else "0x1", "0x0"])
            approved = Bootstrap(approval_profile, approval_state).apply()
            assert approved["status"] == "VERIFIED"
            assert approved["manifest"]["values"]["observed_allowance"] == "7"
            anchor = Journal(approval_state).get("external-approval:approved")["result"]["read_anchor"]
            tx(w3, token.functions.approve(spender, 0))
            w3.provider.make_request("anvil_mine", ["0x80" if chain_id == 1 else "0x2", "0x0"])
            historical = Bootstrap(approval_profile, approval_state).apply(verify=True)
            assert historical["status"] == "VERIFIED"
            assert historical["manifest"]["values"]["observed_allowance"] == "7"
            # Roll back the approval itself, then create a different branch at
            # the same height. Cached approval must not survive that reorg.
            assert w3.provider.make_request("evm_revert", [before_approval])["result"] is True
            tx(w3, token.functions.approve(spender, 8))
            w3.provider.make_request("anvil_mine", ["0x100", "0x0"])
            assert w3.eth.get_block(int(anchor["number"])).hash.hex() != anchor["hash"]
            with pytest.raises(ValueError, match="^EVM_REORG_RECOVERY_REQUIRED$"):
                Bootstrap(approval_profile, approval_state).apply(verify=True)
            record_property("chain_id", chain_id)
            record_property("bootstrap_transactions", 5)
            record_property("concurrent_transactions", 4)
            record_property("external_wait_confirmation_depth", depth)
            record_property("historical_mutable_wait_and_reorg_guard", True)
        finally:
            process.terminate()
            process.wait(timeout=10)

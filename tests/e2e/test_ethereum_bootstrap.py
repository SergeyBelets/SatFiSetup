"""Full V6 public-Bitcoin bootstrap across two EVMs, no public broadcasts.

Real Rust DKG/FROST, real contracts and CLI in separate participant homes.
Bitcoin Core/clock and external economic assets are explicit test fixtures;
this does not claim an Ethereum consensus fork or a funded mainnet BTC payment.
"""
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from web3 import Web3
from eth_account import Account

from test_full_bootstrap import exercise_bootstrap
from test_network_matrix import HistoricalPublicCore
from test_dkg_processes import deploy, tx


@pytest.mark.parametrize("chain_id", [1, 11155111], ids=["ethereum", "sepolia"])
@pytest.mark.parametrize("bitcoin_network", ["mainnet", "testnet4"])
def test_ethereum_mainnet_paid_bootstrap_from_addresses(tmp_path, monkeypatch, record_property, chain_id, bitcoin_network):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    core = HistoricalPublicCore(bitcoin_network)
    rpc = f"http://127.0.0.1:{port}"
    w3 = Web3(Web3.HTTPProvider(rpc))
    log = (tmp_path / "ethereum-node.log").open("w")
    process = subprocess.Popen([shutil.which("anvil"), "--host", "127.0.0.1", "--port", str(port),
        "--chain-id", str(chain_id), "--hardfork", "osaka", "--gas-limit", "30000000",
        "--max-persisted-states", "100000", "--cache-path", str(tmp_path / "anvil-cache"),
        "--timestamp", str(core.record(core.height)["time"] + 1), "--silent"], stdout=log, stderr=log)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError((tmp_path / "ethereum-node.log").read_text())
            if w3.is_connected():
                break
            time.sleep(.05)
        else:
            raise AssertionError("isolated Ethereum node did not start")
        assert w3.eth.chain_id == chain_id
        token = deploy(w3, "StageRewardToken" if chain_id == 1 else "DAOTestUSD")
        feed = deploy(w3, "TWAPRemediationFeed" if chain_id == 1 else "DAOMockFeed")
        if chain_id != 1:
            assert token.functions.decimals().call() == 6
            assert feed.functions.decimals().call() == 8
        operator = Account.from_key((1).to_bytes(32, "big")).address
        tx(w3, token.functions.mint(operator, 20_000_000))
        w3.provider.make_request("anvil_mine", ["0x80", "0x0"])
        policy = tmp_path / "bitcoin-policy.json"
        policy.write_text(json.dumps(core.public_policy()))
        cookie = tmp_path / "fixture-cookie"
        cookie.write_text("fixture:local-only")
        cookie.chmod(0o600)
        # These are explicit synthetic test budgets, never financial defaults.
        for name, value in dict(SATOSHIFI_MAX_FEE_PER_GAS_WEI="3000000000",
                                SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI="1000000000",
                                SATOSHIFI_MAX_TX_GAS=str(2**24)).items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr("test_full_bootstrap.RPC", rpc)
        monkeypatch.setattr("signer_agent.bitcoin_network.time", SimpleNamespace(time=lambda: core.record(core.height)["time"] + 1))
        exercise_bootstrap(tmp_path, record_property, ethereum=dict(core=core, policy=policy,
            cookie=cookie, token=token, feed=feed, chain_id=chain_id, bitcoin_network=bitcoin_network,
            wrapper=Path(__file__).with_name("ethereum_cli_fixture.py")))
        record_property("ethereum_mainnet_scope", json.dumps(dict(
            chain_id=chain_id, bitcoin_network=bitcoin_network, hardfork="osaka", real_contracts=True, real_rust_crypto=True,
            consensus="local Anvil finalized tag, not public Ethereum consensus", bitcoin="historical Core simulation",
            external_economics="local token/feed fixtures; no real funds", transport="on-chain only",
            canonical_test_usd=chain_id != 1, canonical_mock_feed=chain_id != 1,
            reward_token_decimals=token.functions.decimals().call(), feed_decimals=feed.functions.decimals().call(),
            live_mainnet_transactions=False)))
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()

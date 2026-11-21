"""Remote launch checks using provider envelopes and a real checkpoint policy."""

import json

import pytest

from signer_agent import launch_configurator as launch, launch_rpc as rpc
from signer_agent.bitcoin_rpc import BitcoinRPCError
from test_launch_configurator import complete
from test_zk_testnet4 import relay_policy


class Sepolia:
    def __init__(self):
        self.calls = []
        self.errors = {}
        self.block_hash = "0x" + "12" * 32

    def make_request(self, method, params):
        self.calls.append((method, params))
        if method in self.errors:
            return {"error": self.errors[method]}
        block = dict(hash=self.block_hash, number="0x64", transactions=[])
        results = {
            "eth_chainId": "0xaa36a7", "eth_blockNumber": "0x64", "eth_getBlockByNumber": block,
            "eth_getTransactionByHash": None, "eth_getTransactionReceipt": None,
            "eth_getCode": "0x", "eth_getBalance": "0x0", "eth_getTransactionCount": "0x0",
            "eth_gasPrice": "0x1", "eth_maxPriorityFeePerGas": "0x1",
            "eth_call": "0x", "eth_estimateGas": "0x5208", "eth_getLogs": [],
            "eth_simulateV1": [{"calls": [{"status": "0x1", "gasUsed": "0x5208"}]}] * 2,
        }
        if method == "eth_sendRawTransaction":
            assert params == ["0x"]
            return {"error": {"code": -32000, "message": "rlp: invalid transaction"}}
        return {"result": results[method]}


class Core:
    is_https = True

    def __init__(self, policy):
        self.policy = policy
        self.calls = []
        self.errors = {}
        self.header = policy["checkpoint_header"]
        self.anchor = rpc.header_hash(bytes.fromhex(self.header))[::-1].hex()
        self.genesis = "01" * 80
        self.genesis_hash = rpc.header_hash(bytes.fromhex(self.genesis))[::-1].hex()
        self.txid = "45" * 32
        self.tip = "34" * 32

    def call(self, method, *params):
        self.calls.append((method, params))
        if method in self.errors:
            raise BitcoinRPCError("untrusted-provider-secret", self.errors[method], 200)
        if method == "getblock":
            if params == (self.genesis_hash, 0):
                return self.genesis + "01"
            assert params == (self.anchor, 1)
            return dict(hash=self.anchor, height=2016, tx=[self.txid])
        if method == "getrawtransaction":
            assert params == (self.txid, True)
            return dict(txid=self.txid, blockhash=self.anchor, hex="00")
        if method == "decoderawtransaction":
            assert params == ("00",)
            return dict(txid=self.txid)
        if method == "gettxoutproof":
            assert params == ([self.txid], self.anchor)
            return self.header + "01"
        if method == "verifytxoutproof":
            assert params == (self.header + "01",)
            return [self.txid]
        if method == "gettxout" or (method, params) == ("scantxoutset", ("status",)):
            return None
        if method == "getindexinfo":
            return {"basic block filter index": dict(synced=True, best_block_height=2200)}
        if method == "scanblocks":
            assert params == ("start", ["raw(51)"], 2016, 2016, "basic")
            return dict(completed=True, from_height=2016, to_height=2016, relevant_blocks=[])
        if method == "getbestblockhash":
            return self.tip
        if method == "getblockcount":
            return 2200
        code, expected = {
            "getmempoolentry": (-5, (self.txid,)),
            "scantxoutset": (-5, ("start", [{"desc": "raw(not-hex)"}])),
            "testmempoolaccept": (-22, (["00"],)),
            "sendrawtransaction": (-22, ("00",)),
        }[method]
        assert params == expected
        raise BitcoinRPCError("BITCOIN_RPC_REJECTED", code)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    policy = relay_policy()
    policy_path = tmp_path / "checkpoint.json"
    policy_path.write_text(json.dumps(policy))
    config = complete()
    config.update(rpc_profile=rpc.REMOTE_PROFILE, ethereum_network="sepolia", bitcoin_network="testnet4",
        bitcoin_policy_file=str(policy_path),
        reward_token=dict(address=config["treasury"], runtime_hash="0x" + "42" * 32, decimals="6"))
    evm, core = Sepolia(), Core(policy)
    monkeypatch.setenv("RPC", "https://sepolia.infura.io/v3/FakeSecret1")
    monkeypatch.setenv("BTC_RPC", "https://btc.example.invalid/FakeSecret2")
    monkeypatch.setenv("BTC_COOKIE", str(tmp_path / "FakeSecret3"))
    monkeypatch.delenv("SATOSHIFI_CREATE_SIMULATION_RPC", raising=False)
    monkeypatch.setattr(rpc, "HTTPProvider", lambda *a, **kw: evm)
    monkeypatch.setattr(rpc, "BitcoinRPC", lambda *a: core)
    monkeypatch.setattr(rpc, "check_core", lambda *a: dict(blocks=2200, bestblockhash=core.tip))
    return config, policy, evm, core


@pytest.mark.parametrize("watch_height", ["0", "2100"])
def test_preflight_probes_selected_watch_mode_and_uses_invalid_broadcast_payloads(remote, watch_height):
    config, policy, evm, core = remote
    config["bitcoin_watch_start_height"] = watch_height
    report = rpc.check_rpc(config, dict(policy, genesis_hash=core.genesis_hash))
    assert report["status"] == "LAUNCH_RPC_CHECKED"
    assert report["sends_transactions"] is False
    assert ("scanblocks" in report["testnet4"]["methods"]) == (watch_height != "0")
    assert "scantxoutset" in report["testnet4"]["methods"]
    assert "eth_sendRawTransaction" in report["sepolia"]["methods"]
    assert "FakeSecret" not in json.dumps(report)


def test_method_not_found_and_provider_text_are_handled_before_build_writes(remote, tmp_path, capsys, monkeypatch):
    config, _, _, core = remote
    core.errors["scantxoutset"] = -32601
    monkeypatch.setattr(launch, "check_rpc", lambda c,p: rpc.check_rpc(c, dict(p, genesis_hash=core.genesis_hash)))
    cfg = tmp_path / "launch.json"
    cfg.write_text(json.dumps(config))
    out = tmp_path / "out"
    assert launch.main(["build", "--config", str(cfg), "--output", str(out)]) == 2
    captured = capsys.readouterr()
    error = json.loads(captured.out)
    assert not out.exists() and not captured.err
    assert (error["network"], error["method"], error["rpc_error_code"]) == ("testnet4", "scantxoutset", -32601)
    assert "secret" not in captured.out.lower()


@pytest.mark.parametrize("backend", ["relay", "zk-state-v2"])
def test_build_and_participant_check_reuse_the_public_checkpoint(remote, tmp_path, capsys, monkeypatch, backend):
    config, _, _, _ = remote
    config["bitcoin_backend"] = backend
    checked = []
    def preflight(c, policy):
        checked.append(policy)
        return {"status": "LAUNCH_RPC_CHECKED"}
    monkeypatch.setattr(launch, "check_rpc", preflight)
    out = tmp_path / "out"
    launch.build(config, out)
    invite = out / "invitation.json"
    assert json.loads(invite.read_text())["bitcoin_policy"] == checked[0]
    assert "rpc_profile" not in json.loads(invite.read_text())
    assert json.loads((out/"review.json").read_text())["rpc_preflight"]["status"] == "LAUNCH_RPC_CHECKED"
    assert all("FakeSecret" not in p.read_text() for p in out.glob("*.json"))
    # Other participants lack the operator's local source policy.
    (tmp_path / "checkpoint.json").unlink()
    assert launch.main(["check-rpc", "--config", str(out/"launch-config.json"), "--invite", str(invite)]) == 0
    assert checked == [checked[0], checked[0]]
    assert json.loads(capsys.readouterr().out)["status"] == "LAUNCH_RPC_CHECKED"


def test_evm_provider_failure_stops_before_bitcoin_and_redacts_message(remote):
    config, policy, evm, core = remote
    evm.errors["eth_chainId"] = {"code": -32601, "message": "https://sepolia.infura.io/v3/FakeSecret1"}
    with pytest.raises(rpc.PreflightError) as exc:
        rpc.check_rpc(config, policy)
    assert exc.value.details["method"] == "eth_chainId"
    assert "FakeSecret" not in json.dumps(exc.value.details)
    assert not core.calls


def test_alternate_simulator_must_match_sepolia_block_before_running(remote):
    config, _, evm, _ = remote
    alternate = Sepolia()
    assert rpc._evm_checks(evm, config, alternate)["create_simulation"] == "alternate_rpc"
    assert any(method == "eth_simulateV1" for method, _ in alternate.calls)
    assert not any(method == "eth_simulateV1" for method, _ in evm.calls)
    alternate.errors["eth_simulateV1"] = {"code": -32601, "message": "unavailable"}
    with pytest.raises(rpc.PreflightError) as exc:
        rpc._evm_checks(evm, config, alternate)
    assert exc.value.details["method"] == "eth_simulateV1"
    assert exc.value.details["rpc_error_code"] == -32601
    alternate.calls.clear()
    alternate.block_hash = "0x" + "34" * 32
    with pytest.raises(rpc.PreflightError) as exc:
        rpc._evm_checks(evm, config, alternate)
    assert exc.value.details["reason"] == "CREATE_SIMULATION_RPC_CHAIN_MISMATCH"
    assert not any(method == "eth_simulateV1" for method, _ in alternate.calls)


def test_remote_profile_is_bound_to_sepolia_and_testnet4():
    with pytest.raises(ValueError, match="^LAUNCH_RPC_PROFILE_NETWORKS$"):
        launch.validate(dict(complete(), rpc_profile=rpc.REMOTE_PROFILE))
